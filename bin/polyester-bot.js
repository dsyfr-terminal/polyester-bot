#!/usr/bin/env node
"use strict";

const args = process.argv.slice(2);
const target = args[0] === "setup" ? "polyester_bot.setup" : "polyester_bot.monitor";

require("./venv").run(target, args[0] === "setup" ? args.slice(1) : args);
