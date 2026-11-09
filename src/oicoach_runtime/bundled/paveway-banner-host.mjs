// Fixed foreground preload: install the welcome policy before OMP's CLI prepaint.
// No terminal writes, runtime requests, settings discovery, or admission bypass.
import {lstatSync, readFileSync} from "node:fs";
import {createBanner} from "./paveway-banner.mjs";

const file = process.env.PAVEWAY_NATIVE_DATA;
const info = lstatSync(file);
if (!info.isFile() || info.isSymbolicLink() || info.uid !== process.getuid() || (info.mode & 0o077))
    throw new Error("Paveway banner requires private native launch data");
const config = JSON.parse(readFileSync(file, "utf8"));
const resolve = path => Bun.resolveSync("@oh-my-pi/pi-tui/" + path, process.argv[1]);
const {setWelcomePresentation} = await import(resolve("prompt/welcome-presentation"));
const {visibleWidth, truncateToWidth} = await import(resolve("utils"));
const themes = await import(resolve("theme/theme"));
// Access the live theme: final settings can replace the speculative prepaint theme.
const ui = {visibleWidth, truncateToWidth, get theme() { return themes.theme; }};
setWelcomePresentation({introMs: 700,
    render: createBanner({version: config.paveway_version, workflow: config.workflow_id}, ui)});
