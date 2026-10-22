"""Ana ajan döngüsü (Claude Code / Codex tarzı).

user mesajı -> model (araçlarla) -> tool_calls varsa izin kontrolü + çalıştır ->
sonuçları geri ver -> model artık araç çağırmayınca tur biter.
"""

from __future__ import annotations

import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Protocol

from alice.config import Config
from alice.decider import Decider
from alice.permissions import Permissions
from alice.prompts import COMPACT_PROMPT, build_system_prompt
from alice.providers.openai_compat import ChatResult, StreamCancelled
from alice.providers.router import AllProvidersFailed, Router, estimate_tokens, strip_images
from alice.tools.base import SEND, Tool, ToolContext, ToolError, ToolResult, check_args, truncate, unknown_params


class UI(Protocol):
    """Arayüzün (terminal ya da masaüstü uygulaması) ajan olaylarını gösterme sözleşmesi."""

    def stream_start(self) -> None: ...  # model cevabı başlıyor (ilk parça gelene kadar "düşünüyor")
    def stream_delta(self, text: str) -> None: ...
    # Opsiyonel: def thinking_delta(self, text: str) -> None  (modelin akıl yürütme metni)
    def stream_reset(self) -> None: ...  # model akış ortasında düştü, yedek model baştan yazacak
    def stream_end(self, text: str, source: str) -> None: ...
    def tool_start(self, call_id: str, name: str, summary: str) -> None: ...
    def tool_end(self, call_id: str, name: str, result: ToolResult) -> None: ...
    def ask_permission(self, name: str, summary: str, risk: str) -> str: ...  # y | n | a
    def info(self, msg: str) -> None: ...


class NullUI:
    def __init__(self, approve: str = "y"):
        self.approve = approve
        self.events: list[tuple[str, Any]] = []

    def stream_start(self):
        self.events.append(("start", None))

    def stream_delta(self, text):
        self.events.append(("delta", text))

    def stream_reset(self):
        self.events.append(("reset", None))

    def stream_end(self, text, source):
        if text:
            self.events.append(("text", text))

    def tool_start(self, call_id, name, summary):
        self.events.append(("tool", name))

    def tool_end(self, call_id, name, result):
        self.events.append(("result", result.text))

    def ask_permission(self, name, summary, risk):
        self.events.append(("ask", name))
        return self.approve

    def info(self, msg):
        self.events.append(("info", msg))


class Cancelled(Exception):
    pass


class _Sink:
    def __init__(self, ui: UI, cancel: threading.Event):
        self.ui = ui
        self.cancel = cancel
        self.dead = False  # tur durdurulduysa arkada süren model çağrısı artık arayüze yazmasın
        self.parts: list[str] = []

    def on_delta(self, text: str) -> None:
        if self.dead:
            return
        self.parts.append(text)
        self.ui.stream_delta(text)

    def on_reset(self) -> None:
        if self.dead:
            return
        self.parts.clear()
        self.ui.stream_reset()

    def on_reasoning(self, text: str) -> None:
        fn = getattr(self.ui, "thinking_delta", None)
        if fn is not None and not self.dead:
            fn(text)

    def on_tool(self, name: str, size: int) -> None:
        fn = getattr(self.ui, "tool_delta", None)
        if fn is not None and not self.dead:
            fn(name, size)

    def cancelled(self) -> bool:
        return self.dead or self.cancel.is_set()


def _call_key(call: dict[str, Any]) -> str:
    return call["function"]["name"] + ":" + (call["function"].get("arguments") or "").strip()


REPEAT_FAIL_NOTE = ("\n\n[ALICE notu] Bu çağrı aynı argümanlarla {n}. kez başarısız oldu. Aynısını tekrarlama: hatayı oku, "
                    "kök nedeni düşün, farklı bir araç ya da yaklaşım dene; gerçekten yapılamıyorsa kullanıcıya sor.")
ERROR_STREAK_NOTE = ("\n\n[ALICE notu] Son {n} araç çağrısı hata verdi. Devam etmeden dur ve düşün: hata mesajları ne "
                     "söylüyor, varsayımların (ortam, kabuk sözdizimi, dosya yolu, programın kurulu olup olmadığı) doğru mu? "
                     "Yeni bir yol seç ve neden işe yarayacağını kısaca yaz.")
REPEAT_OK_NOTE = ("\n\n[ALICE notu] Aynı çağrıyı {n} kez yaptın ve sonuç ilerlemiyor gibi. Döngüye girme: farklı bir şey "
                  "dene ya da elindeki bilgiyle işi bitir.")


_TOO_BIG = re.compile(r"istek ~(\d+) token, model sınırı (\d+)")


def lean_specs(specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Araç tanımlarının kısa hali: açıklamanın ilk cümlesi, parametre açıklamaları kırpılmış (dar bağlamlı modeller)."""
    out = []
    for sp in specs:
        fn = dict(sp.get("function") or {})
        desc = (fn.get("description") or "").strip().split("\n")[0]
        fn["description"] = desc[:160]
        params = json.loads(json.dumps(fn.get("parameters") or {}))
        for prop in (params.get("properties") or {}).values():
            if isinstance(prop, dict) and prop.get("description"):
                prop["description"] = str(prop["description"])[:60]
        fn["parameters"] = params
        out.append({**sp, "function": fn})
    return out


def fits_after_compact(errors: list[str]) -> int:
    """Modeller sadece konuşma bağlamlarına sığmadığı için düştüyse, sığacak en büyük bağlam (token); yoksa 0."""
    limits = [int(m.group(2)) for e in errors if (m := _TOO_BIG.search(e))]
    big = [int(m.group(1)) for e in errors if (m := _TOO_BIG.search(e))]
    # Büyük bir model sadece kısa süreliğine meşgulse (birkaç dakikalık bekleme, hız sınırı) konuşmayı kalıcı olarak
    # küçültme: beklemek daha iyi. Saatlik kota beklemesi ise kısa değil.
    for e in errors:
        w = re.search(r"soğumada \((\d+) sn\)", e)
        if (w and int(w.group(1)) < 300) or "rate_limit" in e or "429" in e:
            return 0
    if not limits or max(limits) < 4000 or (big and max(limits) >= max(big)):
        return 0
    return max(limits)


def parse_args(raw: str | dict | None) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw or not raw.strip():
        return {}
    try:
        val = json.loads(raw)
    except json.JSONDecodeError:
        # Bazı modeller sona fazladan metin ya da code fence ekliyor.
        cleaned = re.sub(r"^```(?:json)?|```$", "", raw.strip()).strip()
        m = re.search(r"\{.*\}", cleaned, re.S)
        if not m:
            raise
        val = json.loads(m.group(0))
    if not isinstance(val, dict):
        raise json.JSONDecodeError("argümanlar obje değil", str(raw), 0)
    return val


# Eski sabit özetleme sınırı. Eski config.toml'larda yazılı kaldığı için "auto" sayılır (modelin penceresine göre).
LEGACY_COMPACT_AT = 48000
PLAN_NOTE = ("[PLAN MODU: Şu an sadece okuyup araştırabilirsin; dosya değiştiren, komut çalıştıran ya da ekrana "
             "dokunan araçlar reddedilecek. İşi incele, sonra adım adım net bir uygulama planı yaz ve kullanıcının "
             "onayını bekle.]\n\n")


NUDGE_PREFIX = "[devam kontrolü] "
# Cevabın sonu "yapacağım" diyorsa iş yarım kalmış demektir.
_PROMISE_RX = re.compile(
    r"(yapaca[ğg][ıi]m|edece[ğg]im|ba[şs]l[ıi]yorum|bakaca[ğg][ıi]m|deneyece[ğg]im|ekleyece[ğg]im|d[üu]zeltece[ğg]im|"
    r"[çc]al[ıi][şs]t[ıi]raca[ğg][ıi]m|kontrol edece[ğg]im|yazaca[ğg][ıi]m|kuraca[ğg][ıi]m|indirece[ğg]im|"
    r"\blet me\b|\bi'll\b|\bi will\b|\bnext,? i\b|\bnow i\b)[^.!?]{0,60}[.:…]*\s*$", re.I)


class Agent:
    def __init__(
        self,
        config: Config,
        router: Router,
        tools: list[Tool],
        ctx: ToolContext,
        permissions: Permissions,
        ui: UI | None = None,
        decider: Decider | None = None,
        system_prompt: str | None = None,
        is_child: bool = False,
    ):
        self.config = config
        self.router = router
        self.tools = {t.name: t for t in tools if not (is_child and t.name == "task")}
        self.ctx = ctx
        self.permissions = permissions
        self.ui = ui or NullUI()
        self.decider = decider
        self.is_child = is_child
        self.role = "brain"  # sesli asistan "voice" kullanır: ilk kelimesi en çabuk gelen modeller
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt or build_system_prompt(ctx.cwd)}]
        a = config.agent
        self.max_steps = int(a.get("max_steps", 80))
        self._compact_cfg = a.get("compact_at_tokens", "auto")
        # Üst sınır (0 = yok). Proje ajanları ve kurul için konur: 1M bağlamlı modelde her adım yüz binlerce token
        # gönderiyordu (bir proje ~100M token).
        self.compact_cap = 0
        self._last_prompt: tuple[int, int, Any] | None = None  # (sağlayıcının saydığı token, mesaj sayısı, son mesaj)
        self.output_limit = int(a.get("tool_output_chars", 12000))
        self.keep_images = int(a.get("keep_images", 2))
        self.stream = bool(a.get("stream", True))
        # Araçlarla çalışırken iş bitmeden durursa ("şimdi testleri çalıştıracağım" deyip susmak, todo listesinde
        # bitmemiş adım bırakmak) en fazla bu kadar kez devam etmesi hatırlatılır. 0 = kapalı.
        self.finish_nudges = int(a.get("finish_nudges", 2))
        try:
            from alice.hooks import Hooks

            self.hooks = Hooks.from_config(config, ctx.cwd)  # kullanıcının hook'ları (config.toml / hooks.toml)
        except Exception:  # noqa: BLE001 - bozuk hook ayarı ajanı açılmaz yapmasın
            from alice.hooks import Hooks

            self.hooks = Hooks([], ctx.cwd)
        self.cancel = threading.Event()
        self.busy = False
        self._call_counts: dict[str, int] = {}
        self._fail_counts: dict[str, int] = {}
        self._error_streak = 0
        # Her araç adımından sonra çağrılır (oturumu kaydetmek için): tur takılır ya da çökerse iz kaybolmasın.
        self.on_step: Any = None
        self.on_usage: Any = None  # her model cevabının kullanım bilgisi (token, varsa ücret) buraya da bildirilir
        # Uzun süreli hafıza (alice.memory.LongMemory): oturum kuranlar bağlar; testler ve gözetimsiz görevlerde None.
        self.memory: Any = None
        self.memory_channel = ""
        self._memory_version: Any = -1
        # Geri sarma için: kullanıcı mesajının indeksi -> o turda ilk değişen dosyaların eski hali.
        self.checkpoints: dict[int, dict[str, bytes | None]] = {}
        if not is_child:
            ctx.state["agent"] = self

    # ------------------------------------------------------------------ public
    def tool_specs(self) -> list[dict[str, Any]]:
        """Modele gönderilen araçlar: temel araçlar + bu oturumda açılmış gruplar (bkz. tools/groups.py)."""
        if str(self.config.agent.get("tool_groups", "auto")) == "all":
            return [t.spec() for t in self.tools.values()]
        from alice.tools.groups import visible_tools

        return [t.spec() for t in visible_tools(self.tools, self.ctx.state.setdefault("tool_groups", set()))]

    def _enable_groups_for(self, text: str = "", tool_names: tuple[str, ...] = ()) -> None:
        from alice.tools.groups import group_of, groups_for_text

        enabled: set[str] = self.ctx.state.setdefault("tool_groups", set())
        self.ctx.state["tool_names"] = tuple(self.tools)
        enabled |= groups_for_text(text, self.tools)
        enabled |= {g for g in map(group_of, tool_names) if g}

    def spawn_child(self) -> "Agent":
        return Agent(self.config, self.router, list(self.tools.values()), self.ctx, self.permissions, self.ui,
                     self.decider, system_prompt=self.messages[0]["content"], is_child=True)

    def refresh_memory(self, force: bool = False) -> None:
        """Hafızada yeni/değişen bilgi varsa sistem istemindeki profil bölümünü güncelle."""
        mem = self.memory
        if mem is None or self.is_child:
            return
        try:
            from alice import projects

            scanned = projects.load()[0] if projects.enabled(self.config) else 0.0
            version = (mem.version, scanned)
            if force or version != self._memory_version:
                from alice.prompts import with_memory

                profile = "\n\n".join(b for b in (mem.profile(), projects.brief(self.config)) if b)
                self.messages[0]["content"] = with_memory(self.messages[0]["content"], profile)
                self._memory_version = version
        except Exception:  # noqa: BLE001 - hafıza hatası konuşmayı durdurmasın
            pass

    def _recall(self, user_text: str) -> str:
        if self.memory is None or self.is_child:
            return ""
        try:
            return self.memory.recall(user_text)
        except Exception:  # noqa: BLE001
            return ""

    def _remember_turn(self, user_text: str, reply: str) -> None:
        if self.memory is None or self.is_child:
            return
        try:
            self.memory.record(user_text, reply, self.memory_channel)
            self.memory.learn(user_text, reply)
        except Exception:  # noqa: BLE001
            pass

    def run_turn(self, user_text: str, max_steps: int | None = None, images: list[str] | None = None) -> str:
        self.refresh_memory()
        recall = self._recall(user_text)
        final = self._run_turn(user_text, max_steps, images, recall)
        self._remember_turn(user_text, final)
        return final

    def _run_turn(self, user_text: str, max_steps: int | None, images: list[str] | None, recall: str) -> str:
        from alice.mentions import expand_mentions

        content_text = expand_mentions(user_text, self.ctx.cwd)
        if recall:
            content_text += "\n\n" + recall
        if self.hooks.hooks and not self.is_child:
            from alice.hooks import context, feedback

            extra = []
            if len(self.messages) == 1 and self.hooks.has("SessionStart"):
                extra.append(context(self.hooks.run("SessionStart", {})))
            if self.hooks.has("UserPromptSubmit"):
                res = self.hooks.run("UserPromptSubmit", {"prompt": user_text})
                blocked = feedback(res)
                if blocked:
                    self.ui.info(f"hook mesajı engelledi: {blocked}")
                    return ""
                extra.append(context(res))
            extra = [e for e in extra if e]
            if extra:
                content_text += "\n\n[hook bağlamı]\n" + "\n".join(extra)
        if self.permissions.mode == "plan" and not self.is_child:
            content_text = PLAN_NOTE + content_text
        content: Any = content_text
        if images:
            content = [{"type": "text", "text": content_text}] + [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b}"}} for b in images]
        if not self.is_child:
            self.checkpoints[len(self.messages)] = {}
            self.ctx.state["checkpoint"] = self.checkpoints[len(self.messages)]
        msg: dict[str, Any] = {"role": "user", "content": content}
        if content_text != user_text:
            msg["_display"] = user_text
        if images:
            msg["_user_images"] = len(images)
        self.messages.append(msg)
        self._enable_groups_for(content_text)
        final = ""
        steps = max_steps or self.max_steps
        self.cancel.clear()
        self.busy = True
        self._call_counts.clear()
        self._fail_counts.clear()
        self._error_streak = 0
        sink = _Sink(self.ui, self.cancel)
        self._turn_tools: set[str] = set()
        nudges = 0
        stop_hooks = 3  # Stop hook'u ajanı en fazla bu kadar kez geri çevirebilir (sonsuz döngü olmasın)
        try:
            for _ in range(steps):
                if self.cancel.is_set():
                    raise Cancelled
                self._maybe_compact()
                self._prune_images()
                sink.parts.clear()
                self.ui.stream_start()
                sent = len(self.messages)
                try:
                    res = self._interruptible(self.router.chat, self.role, list(self.messages), tools=self._specs(),
                                              stream=sink if self.stream else None)
                except AllProvidersFailed as e:
                    # Kalan modellerin bağlamı konuşmaya yetmiyorsa: o modele sığacak kadar özetle, bir kez daha dene.
                    room = fits_after_compact(e.errors)
                    if room and estimate_tokens(self.messages[:1], self.tool_specs()) > room * 0.45:
                        self._lean = True  # araç tanımları bile dar modeli dolduruyor: kısa tanımlarla gönder
                    if not room or not self._maybe_compact(force=True, limit=room):
                        raise
                    self.ui.stream_start()
                    sent = len(self.messages)
                    res = self._interruptible(self.router.chat, self.role, list(self.messages), tools=self._specs(),
                                              stream=sink if self.stream else None)
                msg = res.message
                if getattr(self, "_lean", False) and self._roomy(res):
                    self._lean = False  # geniş bağlamlı model geri geldi: tam araç tanımlarına dön
                if self.on_usage is not None:
                    try:
                        self.on_usage(res.usage or {})
                    except Exception:  # noqa: BLE001 - sayaç hatası turu bozmasın
                        pass
                pt = int((res.usage or {}).get("prompt_tokens") or 0)
                self._last_prompt = (pt, sent, self.messages[sent - 1]) if pt else None
                if not self.stream and msg.get("content"):
                    self.ui.stream_delta(msg["content"])
                self.messages.append(msg)
                self.ui.stream_end(msg.get("content") or "", f"{res.provider}:{res.model}")
                if msg.get("content"):
                    final = msg["content"]
                if not msg.get("tool_calls"):
                    if stop_hooks and self.hooks.has("Stop") and not self.is_child:
                        from alice.hooks import feedback

                        back = feedback(self.hooks.run("Stop", {"reply": final}))
                        if back:
                            stop_hooks -= 1
                            self.messages.append({"role": "user", "content": NUDGE_PREFIX + "Stop hook'u işi bitmiş "
                                                  "saymadı, devam et:\n" + back, "_nudge": True})
                            continue
                    nudge = self._unfinished(msg.get("content") or "") if nudges < self.finish_nudges else ""
                    if not nudge:
                        return final
                    nudges += 1
                    self.messages.append({"role": "user", "content": nudge, "_nudge": True})
                    continue
                self._turn_tools.update(c["function"]["name"] for c in msg["tool_calls"])
                self._run_tools(msg["tool_calls"])
                if self.on_step is not None:
                    try:
                        self.on_step()
                    except Exception:  # noqa: BLE001 - kayıt hatası işi durdurmasın
                        pass
            self.ui.info(f"adım limiti ({steps}) doldu; devam etmek için yeni mesaj yaz")
            return final
        except (KeyboardInterrupt, Cancelled, StreamCancelled):
            sink.dead = True
            partial = "".join(sink.parts)
            if partial and not (self.messages[-1]["role"] == "assistant" and self.messages[-1].get("content") == partial):
                self.messages.append({"role": "assistant", "content": partial + " [durduruldu]"})
                self.ui.stream_end(partial, "")
            self._repair_after_interrupt()
            self.ui.info("durduruldu")
            return final
        except AllProvidersFailed as e:
            self.ui.stream_end("", "")
            self._repair_after_interrupt()
            self.ui.info(str(e))
            return final
        except Exception:
            # Beklenmeyen hata: cevapsız tool_calls geçmişte kalırsa sonraki her tur sağlayıcıdan 400 alır.
            sink.dead = True
            self._repair_after_interrupt()
            raise
        finally:
            self.busy = False

    def _unfinished(self, text: str) -> str:
        """Tur araçlarla iş yaparken cevap verip durduysa ve iş bitmemiş görünüyorsa modele gidecek hatırlatma (boş:
        bitti). Sesli rolde yok: ikinci cevap da sesli okunurdu."""
        used = getattr(self, "_turn_tools", set())
        if not used or self.role == "voice" or self.permissions.mode == "plan":
            return ""  # plan modunda "şunu yapacağım" zaten istenen cevap
        tail = text.strip()[-240:]
        if tail.endswith("?"):
            return ""  # kullanıcıya soru soruyor: beklemesi doğru
        todos = self.ctx.state.get("todos") or []
        left = [t.get("content", "") for t in todos if isinstance(t, dict) and t.get("status") != "done"]
        if "todo_write" in used and left:
            return (NUDGE_PREFIX + "Todo listesinde bitmemiş adımlar var: " + "; ".join(left[:6]) + ". Kullanıcıdan bir "
                    "şey gerekmiyorsa araçlarla kaldığın yerden devam et. Gerçekten bittiyse listeyi güncelle ve "
                    "sonucu söyle; takıldıysan neden takıldığını açıkça söyle.")
        if _PROMISE_RX.search(tail):
            return (NUDGE_PREFIX + "Yapacağını söyledin ama yapmadan durdun. Kullanıcıdan bir şey gerekmiyorsa şimdi "
                    "araçlarla yap ve bitince sonucu bildir; gerekiyorsa açıkça sor.")
        return ""

    def stop(self) -> None:
        """Başka bir thread'den (ör. uygulamadaki Durdur butonu) mevcut turu durdur. Model ya da araç cevabı
        beklenmez; çalışan komutlar da sonlandırılır."""
        self.cancel.set()
        from alice.tools.shell import kill_foreground

        kill_foreground(self.ctx)

    def _interruptible(self, fn, *args: Any, **kwargs: Any) -> Any:
        """fn'i ayrı bir thread'de çalıştır ve bitmesini beklerken durdurma isteğine bak. Durdurulursa beklemeden
        Cancelled fırlatır; arkadaki çağrı kendi başına biter ve sonucu atılır."""
        box: dict[str, Any] = {}
        done = threading.Event()

        def work() -> None:
            try:
                box["res"] = fn(*args, **kwargs)
            except BaseException as e:  # noqa: BLE001 - çağıran thread'de yeniden fırlatılır
                box["err"] = e
            finally:
                done.set()

        threading.Thread(target=work, daemon=True, name="alice-call").start()
        while not done.wait(0.1):
            if self.cancel.is_set():
                raise Cancelled
        if "err" in box:
            raise box["err"]
        return box["res"]

    def rewind(self, msg_index: int) -> tuple[str, list[str]]:
        """Konuşmayı msg_index'teki kullanıcı mesajından öncesine geri sar ve o andan beri
        ALICE'in dosya araçlarıyla değiştirdiği dosyaları eski haline getir.
        (Shell komutlarının yaptığı değişiklikler izlenemez.) Mesajın metnini ve geri yüklenen dosyaları döndürür."""
        if self.busy:
            raise RuntimeError("çalışırken geri sarılamaz")
        if not 0 < msg_index < len(self.messages) or self.messages[msg_index].get("role") != "user":
            raise ValueError("geçersiz mesaj")
        original: dict[str, bytes | None] = {}
        for idx in sorted(k for k in self.checkpoints if k >= msg_index):
            for path, data in self.checkpoints[idx].items():
                original.setdefault(path, data)
        restored = []
        for path, data in original.items():
            p = Path(path)
            try:
                if data is None:
                    if p.exists():
                        p.unlink()
                else:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(data)
                restored.append(path)
            except OSError as e:
                self.ui.info(f"geri yüklenemedi: {path} ({e})")
        m = self.messages[msg_index]
        text = m.get("_display") or (m["content"] if isinstance(m["content"], str) else
                                     next((p.get("text", "") for p in m["content"] if p.get("type") == "text"), ""))
        if text.startswith(PLAN_NOTE):
            text = text[len(PLAN_NOTE):]
        self.messages = self.messages[:msg_index]
        for k in [k for k in self.checkpoints if k >= msg_index]:
            del self.checkpoints[k]
        return text, restored

    # ------------------------------------------------------------------- tools
    def _run_tools(self, calls: list[dict[str, Any]]) -> None:
        # Model gönderilmemiş bir grubun aracını çağırdıysa (sistem isteminden biliyor) grubu açık tut.
        self._enable_groups_for(tool_names=tuple(c["function"]["name"] for c in calls))
        results = self._execute_calls(calls)
        pending_images: list[tuple[str, list[str]]] = []
        for call, result in zip(calls, results, strict=True):
            name = call["function"]["name"]
            text = truncate(result.text, self.output_limit)
            if result.is_error:
                text = f"HATA: {text}"
            text += self._loop_note(call, result)
            if result.images:
                if self._brain_sees_images():
                    pending_images.append((name, result.images))
                else:
                    text += "\n\n[görüntü açıklaması - vision modeli]\n" + self._describe_images(result.images)
            self.messages.append({"role": "tool", "tool_call_id": call["id"], "name": name, "content": text})
        for name, images in pending_images:
            parts: list[dict[str, Any]] = [{"type": "text", "text": f"[{name} aracının döndürdüğü görüntü]"}]
            parts += [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b}"}} for b in images]
            self.messages.append({"role": "user", "content": parts, "_image": True})

    def _loop_note(self, call: dict[str, Any], result: ToolResult) -> str:
        """Zayıf modeller aynı hatalı çağrıyı tekrar tekrar yapıp takılıyor; bunu fark edip modele söyle."""
        if self.cancel.is_set():
            return ""
        key = _call_key(call)
        self._call_counts[key] = self._call_counts.get(key, 0) + 1
        if result.is_error:
            self._error_streak += 1
            self._fail_counts[key] = self._fail_counts.get(key, 0) + 1
            if self._fail_counts[key] >= 2:
                return REPEAT_FAIL_NOTE.format(n=self._fail_counts[key])
            if self._error_streak >= 3 and self._error_streak % 3 == 0:
                return ERROR_STREAK_NOTE.format(n=self._error_streak)
            return ""
        self._error_streak = 0
        n = self._call_counts[key]
        if n >= 3 and call["function"]["name"] not in ("todo_write",):
            return REPEAT_OK_NOTE.format(n=n)
        return ""

    def _execute_calls(self, calls: list[dict[str, Any]]) -> list[ToolResult]:
        """Çağrıları sırayla işle; art arda gelen, izin gerektirmeyen ve paralel güvenli (salt okuma)
        çağrıları aynı anda çalıştır. Arayüz olayları hep bu thread'den ve çağrı sırasıyla gider."""
        results: list[ToolResult | None] = [None] * len(calls)
        i = 0
        while i < len(calls):
            if self.cancel.is_set():
                results[i] = ToolResult("durduruldu (kullanıcı kesti)", is_error=True)
                i += 1
                continue
            prep = self._prepare(calls[i])
            if isinstance(prep, ToolResult):
                results[i] = prep
                i += 1
                continue
            batch = [(i, prep)]
            j = i + 1
            if self._parallel_ok(prep):
                while j < len(calls) and not self.cancel.is_set():
                    nxt = self._prepare(calls[j], quiet=True)
                    if isinstance(nxt, ToolResult) or not self._parallel_ok(nxt):
                        break
                    batch.append((j, nxt))
                    j += 1
            if len(batch) == 1:
                results[i] = self._execute(calls[i], *prep)
            else:
                for idx, (tool, _args, summary) in batch:
                    self.ui.tool_start(calls[idx]["id"], tool.name, summary)
                with ThreadPoolExecutor(max_workers=min(8, len(batch))) as pool:
                    futures = {idx: pool.submit(self._run_tool, tool, args) for idx, (tool, args, _) in batch}
                    for idx, (tool, _, _) in batch:
                        results[idx] = futures[idx].result()
                        self.ui.tool_end(calls[idx]["id"], tool.name, results[idx])
            i = j
        return [r if r is not None else ToolResult("çalıştırılmadı", is_error=True) for r in results]

    def _parallel_ok(self, prep: tuple[Tool, dict[str, Any], str]) -> bool:
        tool, args, _ = prep
        try:
            return bool(getattr(tool, "parallel_safe", False)) and self.permissions.decide(tool, args, self.ctx) == "allow"
        except Exception:  # noqa: BLE001 - bozuk argüman: sıralı yolda hata olarak modele dönecek
            return False

    def _prepare(self, call: dict[str, Any], quiet: bool = False) -> ToolResult | tuple[Tool, dict[str, Any], str]:
        """Aracı bul, argümanları çöz, izni al. Hata ya da ret varsa ToolResult döner.
        quiet=True iken izin sorulmaz (paralel gruba alınabilir mi diye bakarken)."""
        name = call["function"]["name"]
        tool = self.tools.get(name)
        if tool is None:
            return ToolResult(f"bilinmeyen araç '{name}'. Mevcut araçlar: {', '.join(self.tools)}", is_error=True)
        try:
            args = parse_args(call["function"].get("arguments"))
        except json.JSONDecodeError as e:
            return ToolResult(f"argümanlar geçerli JSON değil ({e}). Tekrar dene.", is_error=True)
        if not isinstance(args, dict):
            return ToolResult("argümanlar bir JSON nesnesi olmalı ({\"ad\": değer}). Tekrar dene.", is_error=True)
        bad = check_args(tool, args)
        if bad:
            return ToolResult(bad + " Düzeltip tekrar çağır.", is_error=True)
        try:
            summary = tool.summary(args)
            if quiet:
                return tool, args, summary
            verdict = self.permissions.decide(tool, args, self.ctx)
            risk = tool.risk_for(args) if verdict == "ask" else ""
        except Exception as e:  # noqa: BLE001 - zayıf modelin yanlış tipli argümanı (liste, null) turu ve geçmişi bozmasın
            return ToolResult(f"geçersiz argüman: {type(e).__name__}: {e}. Parametre tiplerini düzeltip tekrar dene.",
                              is_error=True)
        if verdict == "deny":
            rule = self.permissions.matching_rule(tool, args, self.ctx, "deny")
            self._log_outward(tool, args, summary, "blocked", f"izin kuralı {rule}" if rule else
                              "plan modu ya da gözetimsiz çalışma")
            if rule:
                res = ToolResult(f"Kullanıcının izin kuralı bunu yasaklıyor ({rule}). Aynı şeyi başka biçimde de deneme; "
                                 "gerekiyorsa kullanıcıya neden gerektiğini söyle.", is_error=True)
            else:
                res = ToolResult("Plan modundasın: bu araç değişiklik yapıyor ve şu an kapalı. Önce planını yaz; "
                                 "kullanıcı onaylayıp modu değiştirince uygularsın.", is_error=True)
            self.ui.tool_start(call["id"], name, summary)
            self.ui.tool_end(call["id"], name, res)
            return res
        if verdict == "ask":
            ans = self.ui.ask_permission(name, summary, risk)
            if ans == "a":
                self.permissions.always_allow.add(name)
            elif ans != "y":
                self._log_outward(tool, args, summary, "rejected")
                return ToolResult("Kullanıcı bu çağrıyı reddetti. Aynı şeyi tekrar deneme; başka yol bul ya da kullanıcıya sor.",
                                  is_error=True)
        return tool, args, summary

    def _execute(self, call: dict[str, Any], tool: Tool, args: dict[str, Any], summary: str) -> ToolResult:
        self.ui.tool_start(call["id"], tool.name, summary)
        if self.hooks.has("PreToolUse"):
            from alice.hooks import feedback

            blocked = feedback(self.hooks.run("PreToolUse", {"args": args}, tool.name))
            if blocked:
                result = ToolResult(f"Kullanıcının hook'u bu çağrıyı engelledi: {blocked}", is_error=True)
                self.ui.tool_end(call["id"], tool.name, result)
                return result
        result = self._run_tool(tool, args)
        if self.hooks.has("PostToolUse"):
            from dataclasses import replace

            from alice.hooks import feedback

            back = feedback(self.hooks.run("PostToolUse", {"args": args, "result": result.text[:4000],
                                                           "is_error": result.is_error}, tool.name))
            if back:
                result = replace(result, text=result.text + "\n\n[hook geri bildirimi]\n" + back)
        self._log_outward(tool, args, summary, "failed" if result.is_error else "sent", result.text)
        self.ui.tool_end(call["id"], tool.name, result)
        return result

    @staticmethod
    def _log_outward(tool: Tool, args: dict[str, Any], summary: str, outcome: str, detail: str = "") -> None:
        """Başkasına giden eylemi (send riski) eylem kaydına yaz: gönderilen, reddedilen, engellenen hepsi."""
        try:
            if tool.risk_for(args) != SEND:
                return
        except Exception:  # noqa: BLE001 - risk hesaplanamadıysa kayıt yüzünden tur bozulmasın
            return
        from alice import actionlog

        actionlog.log(tool.name, summary, outcome, args, detail)

    def _run_tool(self, tool: Tool, args: dict[str, Any]) -> ToolResult:
        # Araçlar bu thread'de çalışır (Playwright ve UI Automation nesneleri thread'e bağlı). Durdurulunca
        # stop() araçların başlattığı komutları öldürür, böylece bekleyen araç da hemen döner.
        try:
            return tool.run(self.ctx, **args)
        except ToolError as e:
            return ToolResult(str(e), is_error=True)
        except TypeError as e:
            return ToolResult(unknown_params(tool, args) or f"geçersiz parametre: {e}", is_error=True)
        except KeyboardInterrupt:
            raise
        except Exception as e:  # noqa: BLE001 - araç hatası ajanı düşürmesin, modele dönsün
            return ToolResult(f"{type(e).__name__}: {e}", is_error=True)

    # ----------------------------------------------------------------- vision
    def _brain_sees_images(self) -> bool:
        ref = self.router.first_available(self.role)
        return bool(ref and self.router.supports_vision(ref))

    def _describe_images(self, images: list[str]) -> str:
        task = next((m["content"] for m in reversed(self.messages)
                     if m["role"] == "user" and isinstance(m.get("content"), str)), "")
        parts: list[dict[str, Any]] = [{"type": "text", "text": (
            "You are the eyes of a computer-use agent. Describe this screenshot precisely for the task below: "
            "visible windows and their titles, focused element, all buttons/links/fields/menus with their exact text "
            "and approximate pixel coordinates (x,y of center in this image), dialogs, errors, and anything relevant. "
            f"Task: {task[:800]}")}]
        parts += [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b}"}} for b in images]
        try:
            res = self.router.chat("vision", [{"role": "user", "content": parts}], max_tokens=1500)
            return res.text
        except (AllProvidersFailed, KeyError) as e:
            return f"(görüntü açıklanamadı: {str(e)[:200]})"

    def _prune_images(self) -> None:
        idx = [i for i, m in enumerate(self.messages) if m.get("_image")]
        for i in idx[:-self.keep_images] if self.keep_images else idx:
            self.messages[i] = {"role": "user", "content": "[eski ekran görüntüsü yer kazanmak için kaldırıldı]", "_image": False}

    # -------------------------------------------------------------- context
    @property
    def compact_at(self) -> int:
        """Özetleme sınırı. "auto": kullanılan modelin bağlam penceresinin %80'i (GLM 5.3 ve Gemini'de ~840K,
        Groq gibi dar ücretsiz katmanlarda sağlayıcı sınırı). Sayı yazılırsa o kullanılır."""
        v = self._compact_cfg
        cap = self.compact_cap or 10**9
        if isinstance(v, (int, float)) and not isinstance(v, bool) and int(v) > 0 and int(v) != LEGACY_COMPACT_AT:
            return min(int(v), cap)
        try:
            ref = self.router.last_ref.get(self.role) or self.router.first_available(self.role)
        except KeyError:
            ref = None
        if not ref:
            return min(LEGACY_COMPACT_AT, cap)
        return min(max(16000, int(self.router.context_limit(ref) * 0.8)), cap)

    @compact_at.setter
    def compact_at(self, value: int) -> None:
        self._compact_cfg = int(value)

    def context_tokens(self) -> int:
        """Bağlamdaki token sayısı. Son istekte sağlayıcının kendi saydığı değer (araç tanımları ve sistem istemi
        dahil) esas alınır, ondan sonra eklenen mesajlar tahminle eklenir; yoksa tamamen tahmin."""
        est = estimate_tokens(self.messages, self.tool_specs())
        lp = self._last_prompt
        if lp and lp[0] > 0 and 0 < lp[1] <= len(self.messages) and self.messages[lp[1] - 1] is lp[2]:
            real = lp[0] + (estimate_tokens(self.messages[lp[1]:]) if lp[1] < len(self.messages) else 0)
            # Tahminden 4 kat küçükse sağlayıcının sayısı güvenilmez (ör. önbellekli kısmı saymayan uç): tahmin kalsın.
            if real * 4 >= est:
                return real
        return est

    def _maybe_compact(self, force: bool = False, limit: int = 0) -> bool:
        """Konuşmayı özetle. limit: konuşmanın sığması gereken model bağlamı (ör. sadece küçük modeller açıkken)."""
        if not force and self.context_tokens() < self.compact_at:
            return False
        window = min(limit, self.compact_at) if limit else self.compact_at
        budget = int(window * 0.35)
        summary_tokens = 2000
        if limit:
            # Dar model: sistem istemi ve araç tanımları sabit yer kaplar; özet ve kalan konuşma geri kalana sığmalı.
            fixed = estimate_tokens(self.messages[:1], self._specs())
            summary_tokens = max(300, min(2000, limit // 8))
            budget = int(limit * 0.85) - fixed - summary_tokens
            if budget < 500:
                return False
        # Kesme noktaları: kullanıcı mesajları ve asistan adımlarının başı (araç çağrısı ile sonucu ayrılmaz).
        # Ajanlar tek görev mesajıyla uzun araç zincirleri yürüttüğü için asistan adımlarından da kesilebilmeli.
        users = [i for i, m in enumerate(self.messages) if i > 1 and m["role"] in ("user", "assistant")
                 and not m.get("_image")]
        if not users:
            return False
        cut = users[-1]
        for i in reversed(users):
            if estimate_tokens(self.messages[i:]) <= budget:
                cut = i
            else:
                break
        old = self.messages[1:cut]
        if not old:
            # Tek turda bile çok büyümüş: büyük araç çıktılarını kısalt.
            changed = False
            for m in self.messages[cut:]:
                if m["role"] == "tool" and isinstance(m.get("content"), str) and len(m["content"]) > 3000:
                    m["content"] = truncate(m["content"], 3000)
                    changed = True
            return changed and bool(limit)
        transcript = []
        for m in strip_images(old):
            c = m.get("content") or ""
            if m.get("tool_calls"):
                c += "\n" + "\n".join(f"-> {tc['function']['name']}({tc['function']['arguments'][:400]})" for tc in m["tool_calls"])
            transcript.append(f"[{m['role']}] {truncate(c, 2500)}")
        self.ui.info("context büyüdü, özetleniyor...")
        try:
            res: ChatResult = self.router.chat("brain", [
                {"role": "system", "content": COMPACT_PROMPT},
                # Özet isteği de modele sığmalı: dar bağlamda dökümün sonu (karakter ≈ 3 token) gönderilir.
                {"role": "user", "content": "\n\n".join(transcript)[-(int(limit * 2.4) if limit else
                                                                       max(120000, self.compact_at * 2)):]},
            ], max_tokens=summary_tokens)
        except AllProvidersFailed as e:
            self.ui.info(f"özetleme başarısız: {e}")
            return False
        head = [self.messages[0], {"role": "user", "content": f"[Önceki konuşmanın özeti]\n{res.text}"}]
        if self.messages[cut]["role"] == "user":  # roller sırayla gitsin
            head.append({"role": "assistant", "content": "Özeti aldım, kaldığım yerden devam ediyorum."})
        self.messages = head + self.messages[cut:]
        self.checkpoints.clear()  # mesaj indeksleri değişti, eski geri sarma noktaları geçersiz
        return True

    def _roomy(self, res: ChatResult) -> bool:
        try:
            limit = self.router.context_limit(f"{res.provider}:{res.model}")
        except Exception:  # noqa: BLE001
            return False
        return limit > estimate_tokens(self.messages[:1], self.tool_specs()) * 3

    def _specs(self) -> list[dict[str, Any]]:
        specs = self.tool_specs()
        return lean_specs(specs) if getattr(self, "_lean", False) else specs

    def compact(self) -> bool:
        return self._maybe_compact(force=True)

    def _repair_after_interrupt(self) -> None:
        """Yarım kalan tool_calls için sahte sonuç ekle, yoksa sonraki istek 400 alır."""
        for i in range(len(self.messages) - 1, 0, -1):
            m = self.messages[i]
            if m["role"] == "assistant" and m.get("tool_calls"):
                answered = {x.get("tool_call_id") for x in self.messages[i + 1:] if x["role"] == "tool"}
                for tc in m["tool_calls"]:
                    if tc["id"] not in answered:
                        self.messages.append({"role": "tool", "tool_call_id": tc["id"], "name": tc["function"]["name"],
                                              "content": "durduruldu (kullanıcı kesti)"})
                break

    def reset(self) -> None:
        self.messages = self.messages[:1]
