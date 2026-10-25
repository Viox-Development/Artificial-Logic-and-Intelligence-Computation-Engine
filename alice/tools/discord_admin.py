"""Discord sunucu yönetimi: kanal izinleri (ör. ses kanallarında ve talep/ticket kanallarında görsel atılabilsin).

Anahtar: DISCORD_BOT_TOKEN (Discord bağlantısıyla aynı bot). Botun sunucuda "Rolleri Yönet" ve "Kanalları Yönet"
izni olmalı; Discord, botun kendisinde olmayan bir izni başkasına verdirmez (Dosya Ekle / Bağlantı Yerleştir).

İzin değiştirme var olan kanal ayarını korur: sadece istenen izin bitleri "izin ver"e eklenir ve "engelle"den
çıkarılır, rolün o kanaldaki diğer ayarlarına dokunulmaz. Varsayılan önizleme: önce ne değişeceği gösterilir,
apply=true ile uygulanır (onay ister).
"""

from __future__ import annotations

import os
import re
import time
from typing import Any, Callable

import httpx

from alice.tools.base import NETWORK, WRITE, Tool, ToolContext, ToolError, ToolResult, obj

API = "https://discord.com/api/v10"
TOKEN_ENV = "DISCORD_BOT_TOKEN"
KINDS = {0: "text", 2: "voice", 4: "category", 5: "announcement", 13: "stage", 15: "forum", 16: "media"}
KIND_IDS = {v: k for k, v in KINDS.items()}
# https://discord.com/developers/docs/topics/permissions
PERMS = {"attach_files": 1 << 15, "embed_links": 1 << 14, "send_messages": 1 << 11, "view_channel": 1 << 10,
         "add_reactions": 1 << 6, "use_external_emojis": 1 << 18, "send_voice_messages": 1 << 46}
MEDIA = ("attach_files", "embed_links")  # "görsel atılabilsin" = dosya ekle + bağlantı önizlemesi
MAX_WAIT = 10.0  # 429'da en fazla bu kadar saniye beklenir


class Discord:
    def __init__(self, token: str, client: httpx.Client | None = None, sleep: Callable[[float], None] = time.sleep):
        self.client = client or httpx.Client(timeout=20.0)
        self.headers = {"Authorization": f"Bot {token}", "User-Agent": "ALICE (discord_admin)"}
        self.sleep = sleep

    def req(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        for _ in range(4):
            try:
                r = self.client.request(method, API + path, headers=self.headers, json=body)
            except httpx.HTTPError as e:
                raise ToolError(f"Discord'a ulaşılamadı: {e}") from e
            if r.status_code == 429:
                try:
                    wait = float(r.json().get("retry_after", 1.0))
                except ValueError:
                    wait = 1.0
                if wait > MAX_WAIT:
                    raise ToolError(f"Discord hız sınırı: {wait:.0f} sn sonra tekrar dene")
                self.sleep(wait + 0.1)
                continue
            if r.status_code == 401:
                raise ToolError(f"Discord anahtarı geçersiz ({TOKEN_ENV})")
            if r.status_code == 403:
                raise ToolError("Discord izin vermedi (403): botun sunucuda 'Rolleri Yönet' + 'Kanalları Yönet' "
                                "izni ve kendisinde Dosya Ekle / Bağlantı Yerleştir izni olmalı; kanalda botun rolü "
                                "engellenmiş de olabilir")
            if r.status_code >= 400:
                raise ToolError(f"Discord HTTP {r.status_code}: {r.text[:300]}")
            return r.json() if r.content else None
        raise ToolError("Discord hız sınırı aşılmaya devam ediyor, biraz sonra tekrar dene")


def _bits(perms: list[str] | str | None) -> int:
    names = [perms] if isinstance(perms, str) else list(perms or MEDIA)
    out = 0
    for n in names:
        key = str(n).strip().lower()
        if key not in PERMS:
            raise ToolError(f"bilinmeyen izin: {n}. Geçerli: {', '.join(PERMS)}")
        out |= PERMS[key]
    return out


def select_channels(channels: list[dict[str, Any]], kinds: list[str], match: str = "") -> list[dict[str, Any]]:
    """kinds türündeki kanallar; match verilirse adı ya da kategorisinin adı eşleşenler (eşleşen kategorinin
    kendisi de: bundan sonra açılacak talep kanalları ayarları çoğunlukla kategoriden kopyalar)."""
    want = set()
    for k in kinds:
        if k not in KIND_IDS:
            raise ToolError(f"bilinmeyen kanal türü: {k}. Geçerli: {', '.join(KIND_IDS)}")
        want.add(KIND_IDS[k])
    try:
        rx = re.compile(match, re.I) if match else None
    except re.error as e:
        raise ToolError(f"match geçersiz: {e}") from e
    names = {c["id"]: c.get("name", "") for c in channels}
    out = []
    for c in channels:
        own = rx.search(c.get("name", "")) if rx else None
        parent = rx.search(names.get(c.get("parent_id") or "", "")) if rx else None
        if rx is None:
            ok = c.get("type") in want
        else:
            ok = (c.get("type") in want and (own or parent)) or (c.get("type") == 4 and own)
        if ok:
            out.append(c)
    return sorted(out, key=lambda c: (c.get("type") != 4, c.get("position", 0)))


def merged(overwrites: list[dict[str, Any]], target: str, bits: int) -> tuple[int, int, bool]:
    """(allow, deny, zaten tamam mı): var olan ayara sadece istenen bitler eklenir."""
    cur = next((o for o in overwrites or [] if o.get("id") == target), None)
    allow, deny = (int(cur.get("allow", 0)), int(cur.get("deny", 0))) if cur else (0, 0)
    new_allow, new_deny = allow | bits, deny & ~bits
    return new_allow, new_deny, (new_allow, new_deny) == (allow, deny)


class DiscordAdmin(Tool):
    name = "discord_admin"
    description = (
        "Discord sunucusunda kanal izinlerini toplu düzenle (bot hesabıyla). Örn. bütün ses kanallarında ve "
        "talep/ticket kanallarında herkes görsel atabilsin.\n"
        "- servers: botun olduğu sunucular\n"
        "- channels: kanallar (tür ve kategori ile)\n"
        "- allow: seçilen kanallarda bir role izin ver. kinds (voice, text, stage, category, forum...), match "
        "(kanal ya da kategori adında aranır, ör. 'talep|ticket'), role (varsayılan @everyone), permissions "
        "(varsayılan attach_files + embed_links). Önce apply=false ile ne değişeceğini göster, kullanıcı "
        "isterse apply=true ile uygula.")
    parameters = obj({
        "action": {"type": "string", "enum": ["servers", "channels", "allow"],
                   "description": "servers: sunucular, channels: kanallar, allow: izin ver"},
        "guild_id": {"type": "string", "description": "Sunucu id'si; bot tek sunucudaysa boş bırakılabilir."},
        "kinds": {"type": "array", "items": {"type": "string", "enum": sorted(KIND_IDS)},
                  "description": "[allow] kanal türleri, ör. ['voice','stage'] ya da ['text']"},
        "match": {"type": "string", "description": "[allow] kanal/kategori adı için düzenli ifade, ör. 'talep|ticket'"},
        "role": {"type": "string", "description": "[allow] rol adı ya da id'si; varsayılan @everyone"},
        "permissions": {"type": "array", "items": {"type": "string", "enum": sorted(PERMS)},
                        "description": "[allow] verilecek izinler; varsayılan attach_files, embed_links"},
        "apply": {"type": "boolean", "description": "[allow] false: sadece önizleme (varsayılan), true: uygula"},
    }, ["action"])
    risk = NETWORK

    def __init__(self, make: Callable[[str], Discord] | None = None):
        self.make = make or Discord

    def risk_for(self, args: dict[str, Any]) -> str:
        return WRITE if args.get("action") == "allow" and args.get("apply") in (True, "true", "True", 1) else NETWORK

    def run(self, ctx: ToolContext, action: str = "", guild_id: str = "", kinds: list[str] | None = None,
            match: str = "", role: str = "", permissions: list[str] | None = None, apply: Any = False) -> ToolResult:
        token = os.environ.get(TOKEN_ENV, "").strip()
        if not token:
            raise ToolError(f"{TOKEN_ENV} yok: Ayarlar → Bağlantılar → Discord'a bot anahtarını gir "
                            "(discord.com/developers/applications → Bot → Reset Token)")
        dc = self.make(token)
        if action == "servers":
            gs = dc.req("GET", "/users/@me/guilds") or []
            return ToolResult("\n".join(f"{g['name']} (id {g['id']})" for g in gs) or "bot hiçbir sunucuda değil")
        gid = str(guild_id or "").strip() or self._only_guild(dc)
        channels = dc.req("GET", f"/guilds/{gid}/channels") or []
        if action == "channels":
            names = {c["id"]: c.get("name", "") for c in channels}
            rows = [f"{KINDS.get(c.get('type'), c.get('type'))}: {c.get('name')} (id {c['id']})"
                    + (f" · kategori {names.get(c['parent_id'], '?')}" if c.get("parent_id") else "")
                    for c in sorted(channels, key=lambda c: (c.get("parent_id") or "", c.get("position", 0)))]
            return ToolResult("\n".join(rows) or "kanal yok")
        if action != "allow":
            raise ToolError(f"bilinmeyen işlem: {action}")
        if isinstance(kinds, str):
            kinds = [kinds]
        if not kinds and not match:
            raise ToolError("hangi kanallar? kinds (ör. ['voice']) ya da match (ör. 'talep|ticket') ver")
        picked = select_channels(channels, list(kinds or ["text", "voice", "stage", "forum"]), match)
        if not picked:
            raise ToolError("bu seçime uyan kanal yok; action=channels ile kanallara bak")
        target, role_name = self._role(dc, gid, role)
        bits = _bits(permissions)
        perm_names = ", ".join(n for n, b in PERMS.items() if bits & b)
        todo, already = [], []
        for c in picked:
            allow, deny, same = merged(c.get("permission_overwrites") or [], target, bits)
            (already if same else todo).append((c, allow, deny))
        go = apply in (True, "true", "True", 1)
        lines = [f"{role_name} için {perm_names}: {len(todo)} kanal değişecek, {len(already)} kanal zaten tamam"]
        lines += [f"  {KINDS.get(c.get('type'), '?')}: {c.get('name')}" for c, _, _ in todo[:60]]
        if len(todo) > 60:
            lines.append(f"  ... ve {len(todo) - 60} kanal daha")
        if not go:
            if todo:
                lines.append("Önizleme: uygulamak için aynı çağrıyı apply=true ile yap.")
            return ToolResult("\n".join(lines))
        failed, done = [], 0
        for c, allow, deny in todo:
            if ctx.cancelled():
                failed.append(f"{c.get('name')}: durduruldu")
                break
            try:
                dc.req("PUT", f"/channels/{c['id']}/permissions/{target}",
                       {"type": 0, "allow": str(allow), "deny": str(deny)})
                done += 1
            except ToolError as e:
                failed.append(f"{c.get('name')}: {e}")
                if "403" in str(e) or "geçersiz" in str(e):
                    break  # izin/anahtar sorunu: diğer kanallarda da aynı olur
        lines[0] = (f"{role_name} için {perm_names}: {done} kanalda verildi, {len(already)} kanal zaten tamamdı"
                    + (f", {len(todo) - done} kanalda olmadı" if done < len(todo) else ""))
        lines += [f"HATA {f}" for f in failed[:10]]
        return ToolResult("\n".join(lines), is_error=bool(failed) and done == 0)

    @staticmethod
    def _only_guild(dc: Discord) -> str:
        gs = dc.req("GET", "/users/@me/guilds") or []
        if len(gs) == 1:
            return str(gs[0]["id"])
        if not gs:
            raise ToolError("bot hiçbir sunucuda değil: önce botu sunucuna davet et")
        raise ToolError("bot birden çok sunucuda, guild_id ver: " + ", ".join(f"{g['name']} ({g['id']})" for g in gs))

    @staticmethod
    def _role(dc: Discord, gid: str, role: str) -> tuple[str, str]:
        r = (role or "").strip().lstrip("@")
        if not r or r.lower() in ("everyone", "herkes"):
            return gid, "@everyone"  # @everyone rolünün id'si sunucunun id'si
        roles = dc.req("GET", f"/guilds/{gid}/roles") or []
        hit = next((x for x in roles if x["id"] == r or x.get("name", "").lower() == r.lower()), None)
        if hit is None:
            raise ToolError(f"rol yok: {role}. Roller: " + ", ".join(x.get("name", "") for x in roles[:40]))
        return str(hit["id"]), "@" + hit.get("name", r)


TOOLS = [DiscordAdmin()]
