#!/usr/bin/env bun
import * as fs from "node:fs";
import * as path from "node:path";
import { pathToFileURL } from "node:url";

type ConvertGlbToUsdz = (
  input: ArrayBuffer,
  config: Record<string, unknown>,
  options: { outputPath: string },
) => Promise<{ totalBytes?: number; fileCount?: number } | Blob>;

type Args = {
  input: string;
  output: string;
  repo: string;
};

function parseArgs(): Args {
  const argv = process.argv.slice(2);
  const args: Record<string, string> = {};

  for (let i = 0; i < argv.length; i += 1) {
    const key = argv[i];
    const value = argv[i + 1];
    if (!key.startsWith("--") || !value) continue;
    args[key.slice(2)] = value;
    i += 1;
  }

  const defaultRepo = path.resolve(
    import.meta.dir,
    "../../..",
    "gltf2usdz.online",
  );

  return {
    input: args.input || process.env.INPUT_FILE || "",
    output: args.output || process.env.OUTPUT_FILE || "",
    repo: args.repo || process.env.GLTF2USDZ_REPO_DIR || defaultRepo,
  };
}

function requireValue(value: string, name: string): string {
  if (!value) throw new Error(`Missing required value: ${name}`);
  return value;
}

async function main(): Promise<void> {
  const args = parseArgs();
  const input = requireValue(args.input, "input");
  const output = requireValue(args.output, "output");
  const repo = path.resolve(requireValue(args.repo, "repo"));

  if (!fs.existsSync(input)) {
    throw new Error(`Input file does not exist: ${input}`);
  }

  const converterPath = path.join(repo, "src/vendor/webusd/src/converters/gltf/index.ts");
  if (!fs.existsSync(converterPath)) {
    throw new Error(`gltf2usdz.online GLB converter was not found: ${converterPath}`);
  }

  fs.mkdirSync(path.dirname(output), { recursive: true });

  const module = await import(pathToFileURL(converterPath).href);
  const convertGlbToUsdz = module.convertGlbToUsdz as ConvertGlbToUsdz;

  const config = {
    debug: false,
    debugOutputDir: "./debug-output",
    upAxis: "Y",
    metersPerUnit: 0.008,
    preprocess: {
      dequantize: true,
      generateNormals: true,
      prune: true,
      weld: true,
      dedup: false,
      logBounds: false,
      center: false,
      resample: false,
      unlit: false,
      flatten: false,
      metalRough: false,
      join: false,
    },
  };

  const fileBuffer = fs.readFileSync(input);
  const glbBuffer = fileBuffer.buffer.slice(
    fileBuffer.byteOffset,
    fileBuffer.byteOffset + fileBuffer.byteLength,
  );
  const result = await convertGlbToUsdz(glbBuffer, config, { outputPath: output });
  if (!fs.existsSync(output)) {
    throw new Error("WebUSD conversion finished but output file was not created");
  }

  console.log(
    JSON.stringify({
      converter: "gltf2usdz.online/webusd",
      input,
      output,
      repo,
      result,
    }),
  );
}

main().catch((error) => {
  console.error(error instanceof Error ? error.stack || error.message : error);
  process.exit(1);
});
