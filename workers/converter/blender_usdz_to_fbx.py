import os
import sys

import addon_utils
import bpy


def env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def main() -> int:
    input_path = env("INPUT_FILE")
    output_path = env("OUTPUT_FILE")

    print("--- Blender USDZ -> FBX conversion start ---")
    print(f"Input file: {input_path}")
    print(f"Output file: {output_path}")

    if not os.path.exists(input_path):
        raise FileNotFoundError(f"Input file not found: {input_path}")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    bpy.ops.wm.read_factory_settings(use_empty=True)
    enabled_modules = []
    for module_name in ("io_scene_usd", "usd", "io_usd"):
        module = next((m for m in addon_utils.modules() if m.__name__ == module_name), None)
        if module is not None:
            addon_utils.enable(module_name, default_set=True, persistent=True)
            enabled_modules.append(module_name)
    print(f"Enabled USD-related Blender addons: {enabled_modules}")

    if not hasattr(bpy.ops.wm, "usd_import"):
        available = [m.__name__ for m in addon_utils.modules() if "usd" in m.__name__.lower()]
        raise RuntimeError(
            "Blender USD import operator is unavailable. "
            f"Detected USD-related addons: {available}"
        )

    bpy.ops.wm.usd_import(filepath=input_path)
    print("USD/USDZ import succeeded")

    bpy.ops.export_scene.fbx(
        filepath=output_path,
        use_selection=False,
        global_scale=1.0,
        apply_unit_scale=True,
        apply_scale_options="FBX_SCALE_ALL",
        axis_forward="-Z",
        axis_up="Y",
    )
    print("FBX export succeeded")
    print("--- Blender USDZ -> FBX conversion done ---")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # pragma: no cover - executed inside Blender
        print(f"Conversion failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
