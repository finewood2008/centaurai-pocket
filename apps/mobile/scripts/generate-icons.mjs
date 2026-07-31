import path from "node:path";
import { fileURLToPath } from "node:url";

import sharp from "sharp";

const mobileRoot = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
);
const assetsRoot = path.join(mobileRoot, "assets");
const icons = [
  { source: "icon.svg", target: "icon.png", opaque: true },
  { source: "adaptive-icon.svg", target: "adaptive-icon.png", opaque: false },
  { source: "monochrome-icon.svg", target: "monochrome-icon.png", opaque: false },
];

await Promise.all(
  icons.map(({ source, target, opaque }) => {
    let pipeline = sharp(path.join(assetsRoot, source)).resize(1024, 1024);
    if (opaque) {
      pipeline = pipeline
        .flatten({ background: "#151827" })
        .removeAlpha();
    }
    return pipeline
      .png({ compressionLevel: 9 })
      .toFile(path.join(assetsRoot, target));
  }),
);

console.log("CentaurAI Pocket mobile icons generated.");
