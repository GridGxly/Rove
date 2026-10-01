# Third-party notices

Rove is an independent project by Ralph Clavens Love Noel.

It started from [Erga](https://github.com/Adr1an04/erga-mcp), a local-first recruiting assistant maintained by Adrian (`Adr1an04`) and the Erga contributors.

I built this repo because I wanted to take that foundation further for my own use: let a local recruiting agent operate the application browser, ask for missing information, preserve the exact resume and answers it submitted, and track the recruiting process afterward.

Rove is not an official Erga project, is not endorsed by its maintainer, and should not be confused with the upstream repository.

## Erga

Upstream repository:

https://github.com/Adr1an04/erga-mcp

Upstream license: MIT

Original notice:

```text
MIT License

Copyright (c) 2026 Erga MCP contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

The Rove logo and mark in `docs/assets/` are this project's own. This repository does not ship the Erga logo mark or wordmark.

## Other dependencies

Rove is intended to work with independent projects and services including Qwen, Hermes Agent, Playwright/Chromium, optional Playwright MCP tooling, MLX/MLX-VLM, Obsidian, QMD, Discord, and Zoho APIs.

Obsidian is used as the reference human interface for the private local Markdown vault. QMD is used as a local retrieval/indexing layer over that vault in the reference full setup.

Those projects and services keep their own names, trademarks, licenses, and terms. Using them together does not imply endorsement or affiliation.

Update this file when a dependency's license requires explicit attribution or redistribution notices.

## Patchright

The recruiting browser is driven with [Patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python), a fork of Microsoft Playwright, both under the Apache License 2.0.

## Unslop

The drafting rules digest and the built-in list of AI-writing tells in `src/rove/unslop.py` derive from [Unslop](https://github.com/theclaymethod/unslop) by Clayton Kim, MIT License. The scanners themselves run from a local clone when one is configured.

## Humanizer

The `HUMANIZER_HARD` and `HUMANIZER_SOFT` tell lists, the shape checks (connector dashes,
lists of three, repeated sentence openers, questions, curly quotes) and the Humanizer
rules sentence in `src/rove/unslop.py` are a digest of
[Humanizer](https://github.com/blader/humanizer) by Siqi Chen, MIT License
(Copyright (c) 2025 Siqi Chen), which follows Wikipedia's "Signs of AI writing" guide.
No Humanizer code is copied; the lists restate its pattern catalog.
