// Bundles the background script and the chat tab page (plan 08b) into dist/, and copies the
// manifest and the chat page's index.html and chat.css next to them.
// dist/ is what TB loads via "Debug Add-ons" (pick dist/manifest.json). The XPI is plan 15.
import { copyFileSync, mkdirSync, rmSync } from "node:fs";
import { build } from "esbuild";

const outdir = "dist";
rmSync(outdir, { recursive: true, force: true });
mkdirSync(outdir, { recursive: true });

await build({
  entryPoints: {
    "background/main": "src/background/main.ts",
    "ui/chat/main": "src/ui/chat/main.ts",
  },
  outdir,
  bundle: true,
  format: "esm",
  platform: "browser",
  target: "firefox140",
  sourcemap: true,
  logLevel: "warning",
});

copyFileSync("manifest.json", `${outdir}/manifest.json`);
for (const file of ["index.html", "chat.css"]) {
  copyFileSync(`src/ui/chat/${file}`, `${outdir}/ui/chat/${file}`);
}
console.log(
  `built ${outdir}/background/main.js, ${outdir}/ui/chat/{main.js,index.html,chat.css} and ` +
    `${outdir}/manifest.json`,
);
