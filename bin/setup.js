"use strict";

const { spawnSync } = require("child_process");
const fs = require("fs");
const path = require("path");

const root = path.join(__dirname, "..");
const venv = path.join(root, ".venv");

function python() {
  return process.platform === "win32"
    ? path.join(venv, "Scripts", "python.exe")
    : path.join(venv, "bin", "python");
}

function run(cmd, args, label) {
  const r = spawnSync(cmd, args, {
    stdio: ["ignore", "pipe", "pipe"],
    encoding: "utf8"
  });
  if (r.status !== 0) {
    console.error("polyester-bot: " + label + " failed");
    if (r.stderr) console.error(r.stderr.trim());
    console.error("Install it by hand:");
    console.error("  python3 -m venv .venv && .venv/bin/pip install .");
    process.exit(1);
  }
  return r.stdout || "";
}

function findPython() {
  const candidates = process.platform === "win32"
    ? [["py", ["-3"]], ["python", []], ["python3", []]]
    : [["python3", []], ["python", []]];
  for (const [cmd, args] of candidates) {
    const probe = spawnSync(
      cmd,
      args.concat(["-c", "import sys; print(sys.version_info >= (3, 11))"]),
      { encoding: "utf8" }
    );
    if (probe.status === 0 && probe.stdout.trim() === "True") {
      return [cmd, args];
    }
  }
  return null;
}

function ready() {
  if (!fs.existsSync(python())) {
    return false;
  }
  return spawnSync(python(), ["-c", "import polyester_bot"], { encoding: "utf8" }).status === 0;
}

function ensurePython() {
  if (ready()) {
    return python();
  }
  const found = findPython();
  if (!found) {
    console.error("polyester-bot: Python 3.11 or newer is required and was not found.");
    console.error("Install Python 3.11+, then run: npm rebuild polyester-bot");
    process.exit(1);
  }
  console.log("polyester-bot: setting up the Python environment (takes a minute)...");
  run(found[0], found[1].concat(["-m", "venv", venv]),
      "creating the Python environment");
  run(python(), ["-m", "pip", "install", "--upgrade", "--quiet", "pip"],
      "upgrading pip");
  run(python(), ["-m", "pip", "install", "--quiet", root],
      "installing the bot");
  return python();
}

module.exports = { root, venv, python, ensurePython };
