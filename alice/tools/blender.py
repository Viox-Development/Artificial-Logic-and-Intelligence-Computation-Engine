"""Blender: Python (bpy) betiğiyle 3B sahne kurma.

Blender'ın arayüzü UI Automation'a görünmez ve tıklayarak modellemek çok kırılgandır. Bunun yerine
betik Blender'da penceresiz (-b) çalıştırılır, sahne bir .blend dosyasına kaydedilir ve hata
izi, print çıktısı ve sahnedeki nesnelerin listesi modele geri döner. Böylece model betiği
düzeltip tekrar deneyebilir; bitince action=open sahneyi Blender penceresinde açar.
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from alice.config import alice_home
from alice.tools.base import COMPUTER, EXEC, Tool, ToolContext, ToolError, ToolResult, obj, truncate
from alice.tools.shell import track_foreground

IS_WIN = sys.platform == "win32"
MARK = "@@ALICE@@"

# Kullanıcı betiğini saran betik. Yollar argv'den gelir (blender ... --python wrapper -- <args>).
WRAPPER = r'''
import sys, traceback
import bpy

args = sys.argv[sys.argv.index("--") + 1:]
user_script, out_blend, preview = args[0], args[1], args[2]
MARK = "@@ALICE@@"
ok = True
try:
    with open(user_script, encoding="utf-8") as f:
        code = f.read()
    exec(compile(code, "betik.py", "exec"), {"__name__": "__main__", "bpy": bpy})
except Exception:
    ok = False
    print(MARK + "HATA")
    traceback.print_exc(file=sys.stdout)

print(MARK + "SAHNE")
bpy.context.view_layer.update()  # son değişen ölçek/konum boyutlara yansısın
for o in bpy.context.scene.objects:
    loc = tuple(round(v, 2) for v in o.location)
    dim = tuple(round(v, 2) for v in o.dimensions)
    print(f"{o.name} [{o.type}] konum={loc} boyut={dim}")
print(MARK + "SON")

if ok:
    bpy.ops.wm.save_as_mainfile(filepath=out_blend)
    if preview and preview != "-":
        scene = bpy.context.scene
        if scene.camera is None:
            # Kamera yoksa sahneyi çerçeveleyen geçici bir kamera koy (dosyaya kaydedilmez, kayıt yukarıda yapıldı).
            from mathutils import Vector
            meshes = [o for o in scene.objects if o.type == "MESH"]
            # Zemin gibi büyük düz yüzeyleri çerçeveye katma, yoksa asıl nesne küçücük kalır.
            solid = [o for o in meshes if o.dimensions.z > 0.05] or meshes
            pts = [o.matrix_world @ Vector(c) for o in solid for c in o.bound_box]
            if pts:
                lo = Vector((min(v.x for v in pts), min(v.y for v in pts), min(v.z for v in pts)))
                hi = Vector((max(v.x for v in pts), max(v.y for v in pts), max(v.z for v in pts)))
                center, size = (lo + hi) / 2, max((hi - lo).length, 1.0)
                cam = bpy.data.objects.new("OnizlemeKamerasi", bpy.data.cameras.new("OnizlemeKamerasi"))
                scene.collection.objects.link(cam)
                cam.location = center + Vector((1.0, -1.0, 0.7)).normalized() * size * 1.4
                cam.rotation_euler = (center - cam.location).to_track_quat("-Z", "Y").to_euler()
                scene.camera = cam
        if scene.camera is None:
            print(MARK + "NOT no visible object in the scene, no preview taken")
        else:
            scene.render.engine = "BLENDER_WORKBENCH"
            scene.render.resolution_x, scene.render.resolution_y = 640, 400
            scene.render.resolution_percentage = 100
            scene.render.image_settings.file_format = "PNG"
            scene.render.filepath = preview
            bpy.ops.render.render(write_still=True)
sys.exit(0 if ok else 1)
'''


def find_blender(ctx: ToolContext | None = None) -> str:
    cfg = (ctx.config.data.get("blender", {}) if ctx else {}) or {}
    if cfg.get("path") and Path(cfg["path"]).exists():
        return str(cfg["path"])
    w = shutil.which("blender")
    if w:
        return w
    from alice.tools.desktop import find_installed_apps

    exes = [line.split(": ", 1)[1] for line in find_installed_apps("blender")]
    exes = [e for e in exes if e.lower().endswith(".exe")]
    for e in exes:
        if Path(e).name.lower() == "blender.exe":
            return e
    if exes:
        return exes[0]
    raise ToolError("Blender not found. If it is installed, add [blender] path = \"...blender.exe\" to config.toml.")


def _section(out: str, name: str) -> str:
    start = out.find(MARK + name)
    if start < 0:
        return ""
    rest = out[start + len(MARK + name):]
    end = rest.find(MARK)
    return (rest if end < 0 else rest[:end]).strip()


class Blender(Tool):
    name = "blender"
    description = (
        "Builds/edits a 3D scene in Blender with a Python (bpy) script; use this instead of clicking in the Blender "
        "window. run: the script runs without a window and the scene is saved to the .blend file; returns the "
        "traceback, print output and a list of objects (name, type, location, size). If an error comes back, fix the "
        "script and run it again. open: opens the finished scene in a Blender window.\n"
        "Script tips: bpy.ops.mesh.primitive_cube_add(size=..., location=(x,y,z)); obj = bpy.context.active_object; "
        "obj.scale = (sx,sy,sz); material: mat = bpy.data.materials.new('name'); mat.diffuse_color = (r,g,b,1). The z "
        "axis is up, units are meters."
    )
    parameters = obj({
        "action": {"type": "string", "enum": ["run", "open"], "description": "run: run the script | open: open the "
                                                                             "scene in a Blender window."},
        "code": {"type": "string", "description": "For run: the bpy Python script."},
        "file": {"type": "string", "description": "Path to the .blend. Default: blender/sahne.blend in the ALICE "
                                                  "folder."},
        "new": {"type": "boolean", "description": "true: start from an empty starting scene (it has the default cube, "
                                                  "camera and light; delete them if you don't want them). false: "
                                                  "continue in the previous run's scene."},
        "preview": {"type": "boolean", "description": "Return a small preview image from the camera (with no camera "
                                                      "the scene is framed automatically)."},
        "timeout": {"type": "integer", "description": "Seconds. Default 300."},
    }, ["action"])
    risk = EXEC

    def risk_for(self, args: dict[str, Any]) -> str:
        return COMPUTER if args.get("action") == "open" else EXEC

    def summary(self, args: dict[str, Any]) -> str:
        return f"{args.get('action')} {args.get('file') or ''}".strip()

    def _blend(self, ctx: ToolContext, file: str | None) -> Path:
        if file:
            p = ctx.resolve(file)
            return p if p.suffix.lower() == ".blend" else p.with_suffix(".blend")
        return alice_home() / "blender" / "sahne.blend"

    def run(self, ctx: ToolContext, action: str, code: str | None = None, file: str | None = None,
            new: bool = False, preview: bool = False, timeout: int = 300) -> ToolResult:
        exe = find_blender(ctx)
        blend = self._blend(ctx, file)
        if action == "open":
            if not blend.exists():
                raise ToolError(f"no scene file: {blend}. Create it with action=run first.")
            kwargs: dict[str, Any] = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                                      "stdin": subprocess.DEVNULL}
            if IS_WIN:
                kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
            else:
                kwargs["start_new_session"] = True
            subprocess.Popen([exe, str(blend)], **kwargs)
            return ToolResult(f"opening in the Blender window: {blend}")
        if action != "run":
            raise ToolError(f"unknown action: {action}")
        if not code or not code.strip():
            raise ToolError("run needs code")

        blend.parent.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix="alice-blender-"))
        user = work / "betik.py"
        user.write_text(code, encoding="utf-8")
        wrapper = work / "wrapper.py"
        wrapper.write_text(WRAPPER, encoding="utf-8")
        png = work / "onizleme.png"
        argv = [exe, "-b"]
        argv += ["--factory-startup"] if new or not blend.exists() else [str(blend)]
        argv += ["--python", str(wrapper), "--", str(user), str(blend), str(png) if preview else "-"]

        timeout = max(10, min(int(timeout), 1800))
        with tempfile.TemporaryFile() as fo:
            pop = subprocess.Popen(argv, stdout=fo, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                   env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"})
            try:
                with track_foreground(ctx, pop):
                    rc = pop.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                pop.kill()
                pop.wait()
                fo.seek(0)
                tail = fo.read().decode("utf-8", errors="replace")[-3000:]
                return ToolResult(f"TIMEOUT ({timeout} s): the script did not finish (an endless loop or a very heavy job?)\n{tail}",
                                  is_error=True)
            fo.seek(0)
            out = fo.read().decode("utf-8", errors="replace")

        err = _section(out, "HATA")
        scene = _section(out, "SAHNE")
        notes = [line[len(MARK + "NOT "):] for line in out.splitlines() if line.startswith(MARK + "NOT ")]
        user_out = out.split(MARK, 1)[0]
        # Blender'ın kendi açılış satırlarını at, kullanıcının print'lerini bırak.
        user_out = "\n".join(line for line in user_out.splitlines()
                             if line.strip() and "render | Saved:" not in line
                             and not line.startswith(("Blender ", "Read prefs", "Read blend",
                                                      "found bundled python", "Color management")))
        limit = int(ctx.config.agent.get("tool_output_chars", 12000))
        parts = []
        if err:
            parts.append("SCRIPT ERROR (the scene was not saved; fix the script and run it again):\n" + err)
        elif rc != 0 or not scene:
            parts.append(f"Blender exited with code {rc}:\n" + out[-3000:])
        else:
            parts.append(f"tamam, sahne kaydedildi: {blend}")
        if user_out.strip():
            parts.append("print output:\n" + user_out.strip())
        if scene:
            parts.append("sahnedeki nesneler:\n" + scene)
        parts += notes
        images = []
        if preview and png.exists():
            images.append(base64.b64encode(png.read_bytes()).decode())
        shutil.rmtree(work, ignore_errors=True)
        return ToolResult(truncate("\n\n".join(parts), limit), images=images, is_error=bool(err) or rc != 0)


TOOLS = [Blender()]
