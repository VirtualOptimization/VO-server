"""USDZ-to-GLB runner with a small compatibility fix for connected UV names.

Some USDZ files written by Apple frameworks connect a
``UsdPrimvarReader_float2.inputs:varname`` input to a material interface
attribute.  ``usd2gltf==0.3.5`` only reads the direct value, even though the
connected attribute resolves to a valid UV name (usually ``st``).  Resolve
that value on the in-memory stage before handing it to usd2gltf.
"""

from __future__ import annotations

import argparse

from pxr import UsdShade
from usd2gltf.converter import Converter


def _resolved_input_value(shader_input: UsdShade.Input) -> object | None:
    """Return a direct input value or the value of its producing attribute."""
    value = shader_input.Get()
    if value is not None:
        return value

    for attribute in shader_input.GetValueProducingAttributes():
        value = attribute.Get()
        if value is not None:
            return value
    return None


def normalize_connected_uv_names(stage: object) -> int:
    """Bake connected PrimvarReader UV names into their input on this stage.

    The source USDZ in Object Storage is never modified; only the temporary
    stage used for this conversion is changed.
    """
    normalized_count = 0
    for prim in stage.Traverse():
        shader = UsdShade.Shader(prim)
        if shader.GetShaderId() != "UsdPrimvarReader_float2":
            continue

        varname_input = shader.GetInput("varname")
        if not varname_input or varname_input.Get() is not None:
            continue

        value = _resolved_input_value(varname_input)
        if value is None:
            continue

        # usd2gltf calls Input.Get(), which returns None while a connection
        # remains.  Bake the resolved value into the temporary stage instead.
        varname_input.GetAttr().ClearConnections()
        varname_input.Set(value)
        normalized_count += 1

    return normalized_count


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert USD(Z) to GLB with connected-UV compatibility."
    )
    parser.add_argument("-i", "--input", required=True)
    parser.add_argument("-o", "--output", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    factory = Converter()
    stage = factory.load_usd(args.input)
    normalized_count = normalize_connected_uv_names(stage)
    print(f"Normalized connected UV names: {normalized_count}")
    factory.process(stage, args.output)
    print("Converted!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
