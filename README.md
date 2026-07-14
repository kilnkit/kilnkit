# Kilnkit

> Turn a folder of textures into a render-ready, finished asset — in one click.

![Kilnkit — texture folder to finished PBR asset, in one click](docs/images/cover.png)

**Kilnkit** is a Blender add-on that automates the tedious *finishing* steps of the
PBR material workflow. Point a slot at a texture folder, press one button, and get a
fully wired Principled BSDF material — scale applied, textures mapped, nodes connected,
named, and ready for your asset library.

It is built around one idea: **you create, Kilnkit handles the chores.** Scaling,
unwrapping, node wiring, naming, LODs, and library export are repetitive work — so the
add-on does them for you, with **zero external dependencies** in the core.

## Features

- **One-click PBR** — scale → texture mapping → node graph, wired to Principled BSDF.
  The default is a seam-free triplanar **preview** that works on any shape without touching
  your UVs.
- **Accurate texture detection** — reads a MaterialX (`.mtlx`) sidecar when present to map
  each channel exactly (no keyword guessing, no GL/DX normal ambiguity). Falls back to
  filename keyword detection (works with ambientCG, Poly Haven, Substance, and most
  naming schemes, suffix-independent). Uses only Python's built-in XML — no extra installs.
- **Finish UV, when you need it** — triplanar is a preview: it creates no UV map, so the
  panel says so and offers one click to either **use the mesh's existing UV map** or
  **create one**. Baking, texture painting and engine export all need real UVs.
- **6 UV mapping modes** — Triplanar (preview default, seam-free box projection), Keep
  Existing UV, Object, Cube, Smart UV, and SLIM (auto-seam minimum-stretch unwrap).
- **Live sliders** — texture scale (X/Y/Z), AO, normal, and height update the material in
  real time; optional two-way sync with the node graph.
- **Asset-library workflow** — build materials straight from folders without a mesh,
  register them as assets, and export one packed `.blend` per material to your asset library.
- **Multi-asset cleanup** — merge duplicate materials (by texture set), generate
  non-destructive LODs (`_LOD0..n`), and apply unified naming so objects, meshes, and
  materials share one family name.
- **Batch mode** — apply to many selected objects at once, or import a parent folder and
  get one slot per sub-folder, all without freezing the UI.
- **Render output automation** — set up a studio or HDRI environment (with transparent
  background), three-point lighting, and an auto-framed camera, then render multi-angle
  stills and a 360° turntable video — all from one **Render** tab, so you can present a
  finished asset without leaving Blender.

## Requirements

- **Blender 4.5 LTS** or newer.

## Installation

1. Zip the `kilnkit` folder (or download the release `.zip`).
2. In Blender: **Edit → Preferences → Add-ons → Install…** and select the zip.
3. Enable **Kilnkit** in the add-on list.

The panel appears in the **3D Viewport sidebar** (press **N**) under the **Kilnkit** tab.

> **Language:** the interface is in **English** by default. Korean is also included — switch
> via **Edit → Preferences → Interface → Translation → Language → 한국어**.

## Quick start

1. Open the **Kilnkit** tab in the N-panel.
2. Use **"Select a texture folder to start"** (or point an empty slot at a folder).
3. Press the **Apply** button — Kilnkit applies scale, builds the node graph, and wires the
   material with a seam-free triplanar preview.
4. Adjust the live sliders (texture scale, AO, normal, height) — changes apply instantly.
5. When the asset needs real UVs (baking, texture painting, engine export), press
   **Use Existing UV** or **Create UV Map** in the panel's UV line.

For multiple materials, add a slot per texture folder and assign each to faces. To process
many objects, or to import a parent folder of sub-folders in one go, use the **Batch** tab.

## Screenshots

**One click: texture folder → finished, wired material**

![Before and after applying Kilnkit to a texture folder](docs/images/before_after.png)

**The main panel — slots, detected channels, and live sliders**

![Kilnkit main panel](docs/images/panel_main.png)

**Render tab — environment, camera, and output automation**

![Kilnkit render tab](docs/images/panel_render.png)

**One material set, many looks — same pipeline, any folder**

![Material variety grid rendered with Kilnkit](docs/images/variety_grid.png)

**Multi-angle stills, generated automatically**

![Multi-angle render grid](docs/images/multiangle_grid.png)

## Development

Kilnkit is designed, built and maintained by one person — a Blender user who kept
meeting the same finishing chores in his own scenes, and finally built the tool for them.

Parts of the code were written with the help of an AI coding assistant (Claude Code).
The design decisions, the testing and the release are the author's, and the author is
responsible for the result. Commits made with AI assistance carry an `Assisted-by:`
trailer.

## Support

- **Bugs and feature requests** — <https://github.com/kilnkit/kilnkit/issues>
- **Email** — kilnkithq@gmail.com
- **Website** — <https://kilnkit.com>

## License

Kilnkit is free software, licensed under the **GNU General Public License v3.0 or later**
(GPLv3+). See [LICENSE](LICENSE) for the full text.

Copyright (C) 2026 Deokho Kim
