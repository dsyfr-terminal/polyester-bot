"use strict";

const { spawnSync } = require("child_process");
const { ensurePython } = require("./setup");

function run(moduleName, args) {
  const py = ensurePython();
  const r = spawnSync(py, ["-m", moduleName].concat(args), { stdio: "inherit" });
  process.exit(r.status === null ? 1 : r.status);
}

module.exports = { run };
