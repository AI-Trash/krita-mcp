# Krita MCP Server

Let an MCP client paint in [Krita](https://krita.org/), manage layers and selections,
and inspect non-destructive PNG previews. This project has **no drawing CLI**: the
`krita-mcp` binary runs the stdio MCP server (or prints build metadata with
`krita-mcp --version`).

## Architecture

```text
MCP client -> krita-mcp (Go stdio binary) -> loopback HTTP -> Krita Python plugin
```

- `cmd/krita-mcp/`: standalone Go entrypoint (`krita-mcp` for stdio MCP, `krita-mcp --version` for build info).
- `internal/mcpserver/`: 31 `krita_` MCP tool registrations, schemas, and legacy string or structured JSON result formatting.
- `internal/kritaclient/`: strict Go command/batch validation and single-attempt loopback `net/http` transport.
- `krita-plugin/kritamcp/`: HTTP handoff and native painting on Krita's GUI thread,
  geometry preparation, generated polygon layers and selection masking, and preview export.

All 15 original tool names and the previous 14 extended tools (polygons, layers,
selections, current-brush inspection, and batches) preserve their MCP schemas and
wire contracts. Two additional tools delete layers and merge layers down.
External Python server modules (`server.py`, `krita_mcp`, `krita_client`, and
`requirements.txt`) were intentionally removed in favor of the standalone Go binary.
No model calls are made by the server, and no API keys are required.

## Installation

End users who extract a prebuilt `krita-mcp_<VERSION>_<OS>_<ARCH>.tar.zst` release
archive do not need external Python or Go installed. Extract the `krita-mcp`
executable (`krita-mcp.exe` on Windows) and install the Krita plugin separately.
A mutable development [snapshot release](https://github.com/AI-Trash/krita-mcp/releases/tag/snapshot)
is available. Stable release publication is still pending. Building from source
requires Go 1.25+ (tested with Go 1.26.1).

Use a Krita build exposing native `Node.paintPath` and `Node.paintLine`. Those APIs
were probed on macOS Krita 5.3.4.1 with Krita's bundled Python 3.13 (the plugin
uses only Python standard library modules plus Krita's bundled `PyQt5`/`PyKrita`,
with a minimum of Python 3.11 for `typing.assert_never`, and no external `pip` packages).
That probe is **not** a claim that every complete plugin workflow, installer/importer
path, or native Windows/Linux runtime has been verified in the live application.
There is no raster-brush fallback.

### Install the Krita Plugin

Import `krita-mcp-plugin_<VERSION>.zip` in Krita via **Tools > Scripts > Import Python Plugin...**,
or copy **the entire** `krita-plugin/kritamcp/` directory (every `.py` module) and
`krita-plugin/kritamcp.desktop` into Krita's Python plugin directory:

| OS | Directory |
|----|-----------|
| Windows | `%APPDATA%\krita\pykrita\` |
| Linux | `~/.local/share/krita/pykrita/` |
| macOS | `~/Library/Application Support/krita/pykrita/` |

Do not copy only `__init__.py`: the plugin imports sibling modules. Replace the
old folder as a whole when updating. Enable **Krita MCP Bridge** in
**Settings > Configure Krita > Python Plugin Manager**, then **restart Krita**.
Keep a Krita window open. New documents use RGBA/U8; generated pixels, alpha
selection, and pixel sampling require the document/layer color space **RGBA, 8-bit
unsigned integer (U8)**. Convert incompatible documents before running those operations.

### Build or Install the Go Server

Build from a local checkout (requires Go 1.25+, tested with Go 1.26.1):

```sh
go build -o krita-mcp ./cmd/krita-mcp
```

On Windows, build `-o krita-mcp.exe`. You can also install with `go install`:

```sh
# Development branch:
go install github.com/AI-Trash/krita-mcp/cmd/krita-mcp@dev

# Published release tag (once a vX.Y.Z tag is published):
go install github.com/AI-Trash/krita-mcp/cmd/krita-mcp@vX.Y.Z
```

`go install` places the binary in `$GOBIN` or `$(go env GOPATH)/bin` (typically
`~/go/bin/krita-mcp` on macOS/Linux and `%USERPROFILE%\go\bin\krita-mcp.exe` on
Windows). Builds without release `-ldflags` report default metadata
(`krita-mcp dev (commit none, built unknown)`). `go install` installs only the
external MCP server binary; it does **not** download or install the Krita Python
plugin, which must still be imported from the plugin `.zip` or copied into `pykrita`.

### Configure an MCP Client

For Claude Desktop or another stdio MCP client, pass the absolute executable path
(`C:\\absolute\\path\\to\\krita-mcp.exe` on Windows):

```json
{
  "mcpServers": {
    "krita": {
      "command": "/absolute/path/to/krita-mcp",
      "args": [],
      "env": {"KRITA_URL": "http://127.0.0.1:5678"}
    }
  }
}
```

The server communicates with the plugin using `POST /` and
`{"action": "stroke", "params": {...}}`. `GET /health` and `GET /info` report
plugin health/version and capabilities; the current base plugin version is `0.3.1`.
The HTTP bridge rejects every request carrying an `Origin` header and requires
`Content-Type: application/json` for command POSTs. Browser access is not supported.
These restrictions do not authenticate callers: local processes remain
unauthenticated, so the bridge is not a secure remote service.

| Setting | Default | Configuration |
|---------|---------|---------------|
| Plugin bind | `127.0.0.1` | Loopback only |
| Plugin port | `5678` | `KRITA_MCP_PORT` in Krita's launch environment; restart Krita |
| Client URL | `http://127.0.0.1:5678` | `KRITA_URL` in the MCP server environment |
| Preview output | `~/krita-mcp-output` | Fixed in plugin code; no runtime configuration setting |

Changing the plugin port requires a matching client URL. Application launchers
may not inherit variables set only in an unrelated terminal.

## Tools

Coordinates are canvas pixels with origin at the top-left, x rightward and y
downward. Stroke and polygon coordinates accept finite fractional numbers;
selection rectangles and sampled pixels use integers. Hex colors are `#RGB`,
`#RRGGBB`, or Qt's **`#AARRGGBB`** (alpha first, not CSS RGBA ordering).
Explicit hex colors and sampled RGB values use standard sRGB; generated whole
rasters are converted from standard sRGB to the document ICC profile. Raw pixel
operations still require RGBA/U8. When a generated operation omits its color,
the foreground is read as a managed color, not assumed to use the monitor profile.
Operations on a document are rejected if that same document has multiple views;
views of other documents are allowed.

| Tool | Parameters / Behavior |
|------|-----------------------|
| `krita_health` | Check plugin connectivity |
| `krita_new_canvas` | `width=800, height=600, name='New Canvas', background='#1a1a2e'` |
| `krita_set_color` | `color`: set foreground |
| `krita_set_brush` | `preset=None, size=None, opacity=None`: actual native preset, pixel size, opacity 0..1 |
| `krita_stroke` | `points, pressure=1, size=None, color=None, opacity=None, smooth=True, curve=None, taper_start=0, taper_end=0, preserve_alpha=False` |
| `krita_fill` | `x, y, radius=50`: filled circle, **not** flood fill |
| `krita_draw_shape` | `shape, x, y, width=100, height=100, fill=True, stroke=False, x2=None, y2=None`; rectangle/ellipse/line |
| `krita_fill_polygon` | `points, color=None, smooth=True, feather=0, preserve_alpha=False` |
| `krita_gradient_polygon` | `points, color, gradient_end_color, gradient_type='linear', direction=0, smooth=True, feather=0, preserve_alpha=False` |
| `krita_create_layer` | `name='Paint Layer', blending_mode='normal'`; creates and activates a paint layer |
| `krita_set_layer_properties` | `layer_id=None, name=None, opacity=None, visible=None, blending_mode=None, preserve_alpha=None`; None properties unchanged |
| `krita_list_layers` | Return `layers` with UUIDs and `active_layer_id`; refresh IDs after merging |
| `krita_set_active_layer` | `layer_id`: activate an existing layer |
| `krita_delete_layer` | Required nonempty string `layer_id`: remove the identified layer and its contents; return `deleted_layer_id` and nullable `active_layer_id` |
| `krita_merge_down` | Required nonempty string `layer_id`: merge into the layer immediately below; return a new `layer_id`/`active_layer_id` and the input `merged_layer_id` |
| `krita_select_rect` | `x, y, width, height`: replace selection with rectangle |
| `krita_select_ellipse` | `x, y, width, height`: replace selection with ellipse |
| `krita_select_polygon` | `points, smooth=False`: replace selection with closed polygon |
| `krita_select_by_alpha` | `threshold=1`: active-layer byte alpha, range **1..255** |
| `krita_deselect` | Clear selection using native `setSelection(None)` |
| `krita_fill_selection` | `color=None, preserve_alpha=False`: native active-layer fill; requires selection |
| `krita_get_current_brush` | Return `preset`, native `size`, and `opacity` |
| `krita_get_canvas` | `filename='canvas.png', show_grid=False, grid_spacing=100, diff_with_reference=None` |
| `krita_save` | `path`: `.kra` document save or image export |
| `krita_open_file` | `path`: open supported existing document/image |
| `krita_get_color_at` | `x, y`: sample document projection |
| `krita_list_brushes` | `filter='', limit=20`: list presets by partial name |
| `krita_undo` / `krita_redo` | Request one native history step |
| `krita_clear` | `color='#1a1a2e'`: native active-layer fill, respecting selection/opacity/alpha lock |
| `krita_batch` | `commands`: one or more `{action, params}` entries, no total-count ceiling; sequential and **non-atomic** |

Legacy tools keep string responses (returned as text and wrapped in `{"result": "..."}`
structured content); undo/redo report an unverified history request, and clear
describes native active-layer fill rather than canvas clearing. The extended
`krita_stroke` and `krita_get_canvas`, along with newer tools, return plugin JSON
objects intact so result details are not lost. Failures contain an `error` key.
Both MCP schema validation and Go argument parsing validate inputs before sending
any HTTP request: invalid top-level tool arguments return an MCP tool error
(`isError: true`), while invalid batch child commands return a structured
`{"error": "..."}` object without contacting Krita.

Layer IDs are UUIDs returned by layer operations or `krita_list_layers`.
Deleting the active layer selects an available fallback; `active_layer_id` is
null if no active layer remains. Merging replaces layer identities with a new
UUID and activates the merged layer. Use the returned `layer_id` or refresh
`krita_list_layers` before addressing the merged layer again; the old UUIDs are
no longer valid. Both tools require an explicit ID, reject extra arguments, and
can be used as `delete_layer` and `merge_down` batch actions. A batch cannot
substitute a previous result's new UUID into a later command: split dependent
work into another call after receiving the merge result.

## Native Stroke and Polygon Semantics

Stroke points are `[x,y]` or `[x,y,pressure]`, with at least two points. Scalar
pressure and point pressure are each 0..1 and multiply. `curve=None` uses
Catmull-Rom smoothing when `smooth=True`, otherwise straight segments. Explicit
`curve='linear'` or `'bezier'` overrides smooth; cubic Bezier requires **3n+1**
control points, n >= 1. Tapers are fractions 0..1 of total path length. The current
preset governs pressure-to-width response and hardness; **there is no hardness
argument**. Per-call color/size/opacity and temporary alpha lock are restored.

When scalar and all point pressures equal 1 and no taper is requested, strokes
use one native `paintPath`, including pressure triples whose pressure values are
all 1. Nonunit scalar/point pressure or taper uses sampled native `paintLine`
segments. This is **not a continuous human stroke**: segments reset brush/smudge
state, may change spacing and alpha buildup, round sampled coordinates to
integers, and are expected to produce multiple undo steps. Sampled pressure
chords are bounded to 4 pixels (previously 2); synchronization occurs every
32 native calls plus a final flush, rather than after each segment. This reduces
synchronization overhead while retaining independent native undo entries and
integer-coordinate precision; it does not combine the segments into one undo
step or provide fractional native pressure coordinates. Numeric `undo_steps`
is an **expected, unverified count**, with `undo_steps_verified=False`; native
no-ops can produce a different actual history count. Inspect `engine`, `mode`,
`native_calls`, `undo_steps`, `undo_steps_verified`, and `fidelity_warning` in the result.

Solid unfeathered polygons use native fill on the active layer. Polygon points
are coordinate pairs, at least three distinct vertices; smoothing closes a
Catmull-Rom contour. Feather is 0..256 pixels.

**Every gradient and feathered fill creates an independent new paint layer.**
Raw `setPixelData` on an attached target damaged native undo in API experiments,
so generated data is written to a detached new node before attachment. Results
include `layer_id`, `target_layer_id`, `independent_layer`, and `history` when a
layer is created. There is no automatic merge or promise of preserving the
current layer's alpha: `preserve_alpha=True` is rejected for generated operations.
Active selection clips generated pixels; the target layer's pixels and identity
are left unchanged. An entirely out-of-bounds polygon can return `changed=False`
and `layer_id=None` instead of creating a layer. After attachment, the generated
layer remains active, including within a batch. Creating layer A, generating G,
then creating B places B above G, with G above A. Painting after generating G
targets G until another layer is activated or created.

Gradients accept `linear` or `radial`. Linear direction is degrees: 0 is
left-to-right, 90 top-to-bottom. Radial gradients center on the polygon bounding
box, from `color` in the center to `gradient_end_color` outward. Blending modes
are `normal`, `multiply`, `screen`, `add`, and `overlay`. Layer opacity is 0..1;
`preserve_alpha` on layer properties sets persistent native alpha lock.

## Previews, Batches and Limits

`krita_get_canvas` returns `path`, `width`, `height`, and optional separate
`diff_path`/`reference_path`. Filename must be a PNG basename; a suffixless name
gains `.png`. A grid is applied to the exported **copy**, never document pixels.
Grid spacing is an integer 8..32768 pixels. Reference images must match canvas
dimensions. Diff compares ungridded 8-bit sRGB RGBA: black is identical; red shows
maximum channel error (minimum visible intensity 48). Untagged references are
assumed sRGB. Source documents and reference images are protected from overwrite.

Both client and plugin validate all batch payloads before mutation. Submit the
complete batch in one `action="batch"`, `params.commands` request: there is no
total command-count or aggregate work budget, client-side chunking, job API, or
automatic retry. Only the plugin schedules incremental execution on Krita's main
thread, resuming the active batch across QTimer ticks without interleaving other
commands. A document/view/layer context change stops the batch and reports its
completed prefix. Individual native paint calls cannot pause and are not split into
smaller strokes; they can still block the GUI until they finish. Runtime execution
is sequential, **not atomic**: there is no atomic parameter, snapshot,
rollback, or automatic undo. On failure prior changes remain. Results include
`completed_count`, `results`, `atomic=False`; failures additionally include
`failing_index` and `failing_result`. Nested batches are forbidden. A failed
pressure stroke itself may already have painted some native segments.

Successful batches retain per-command results and their unique query data, but
move `undo_steps_note` and `fidelity_warning` strings into a top-level
`warnings` list. Warning strings appear once, in first-seen order; `warnings` is
omitted when there are none. Failure results and single-command responses keep
their original warning fields. Generated layers becoming active are expected
batch context changes; unrelated document/view/layer changes still stop a batch.

Only batch requests have **no HTTP read timeout**, so long native work can finish.
Their connect and write timeouts stay finite (120 seconds).
Client export/save (`get_canvas` and `save`) timeouts are **120 seconds**, ordinary
commands **30 seconds**, and `health`/`info` **5 seconds**. These Go HTTP client
timeouts are fixed per command category, with no connection-pool wait limit or
runtime timeout override.
Batch requests have no plugin queue deadline; non-batch requests retain a bounded
admission deadline. The queue holds at most 32 pending requests. A queued operation
can be cancelled before execution; an already executing operation may have an
**unknown outcome**. Neither timeouts nor HTTP transport failures are automatically
retried. Inspect Krita and partial results before retrying a mutation. There is no
hard 60-requests/minute throttle.

The whole batch request and accumulated results are retained in memory; removing
total-count/work and HTTP body-size ceilings does **not** promise infinite memory
or rendering resources. Plugin bounds still include 32 pending requests and 4096
points per operation. Existing per-operation raster bounds are distinct: generated
patches/selections/new canvases have a 4M-pixel budget; several BGRA images and
selection masks can coexist in memory. Previews allow 16M pixels, 32768 pixels
per side, 128 MiB encoded images and 2048 grid lines. Native undo/redo requests
return `history_verified=False`: an enabled QAction does not prove history
changed. Their string tools say "Undo requested; history change unverified" or
"Redo requested; history change unverified". Pressure operations may need multiple steps, and
generated layers are not one atomic painting transaction. Save checkpoints.

## Examples

MCP clients invoke tools by `name` and JSON `arguments`:

```json
{
  "name": "krita_new_canvas",
  "arguments": {
    "width": 800,
    "height": 600,
    "background": "#ffffff"
  }
}
```

```json
{
  "name": "krita_stroke",
  "arguments": {
    "points": [[60.5, 180, 0.2], [160, 90, 0.9], [300, 180, 0.4]],
    "pressure": 0.8,
    "smooth": true,
    "taper_start": 0.15,
    "taper_end": 0.25,
    "color": "#345678",
    "preserve_alpha": false
  }
}
```

```json
{
  "name": "krita_gradient_polygon",
  "arguments": {
    "points": [[340, 240], [660, 240], [560, 460]],
    "color": "#ef9950",
    "gradient_end_color": "#246080",
    "gradient_type": "linear",
    "direction": 90,
    "smooth": true,
    "feather": 8
  }
}
```

```json
{
  "name": "krita_get_canvas",
  "arguments": {
    "filename": "progress.png",
    "show_grid": true,
    "grid_spacing": 100,
    "diff_with_reference": "/Users/you/krita-mcp-output/reference.png"
  }
}
```

Batch action names **omit** `krita_` and each entry includes `params`:

```json
{
  "name": "krita_batch",
  "arguments": {
    "commands": [
      {"action": "set_color", "params": {"color": "#226688"}},
      {"action": "stroke", "params": {"points": [[40, 40], [140, 80]], "smooth": true}},
      {"action": "get_canvas", "params": {"filename": "batch.png"}}
    ]
  }
}
```

## Versioning and Releases

The root [`version`](version) file holds a single-line stable SemVer base (`0.3.1`,
without a `v` prefix or prerelease/build suffix). The development
[snapshot prerelease](https://github.com/AI-Trash/krita-mcp/releases/tag/snapshot)
has been published; stable release publication is still pending.

- **`main` (stable releases)**: Pushing to `main` publishes `v<VERSION>` as a
  non-prerelease using `andelf/nightly-release`, as in LavaFishing. Reusing a
  version can advance its tag and replace existing assets; releases are not
  protected against overwrites. Bump [`version`](version) for a new stable release.
- **`dev` (mutable snapshots)**: Pushing to `dev` replaces the `snapshot` tag and
  prerelease with version `<VERSION>-dev.<12-char-commit-sha>` using
  `andelf/nightly-release`. Prereleases are excluded from GitHub's latest stable
  release. New snapshot runs cancel older runs. The action only advances tags
  fast-forward; non-fast-forward history can fail after old assets are removed.
- **Workflow structure**: [`.github/workflows/stable.yml`](.github/workflows/stable.yml)
  and [`.github/workflows/snapshot.yml`](.github/workflows/snapshot.yml) each contain
  one push-only publication job: checkout, Go setup/cache, version extraction,
  UPX installation, plugin packaging, GoReleaser build, and release publication.
  There are no PR triggers or verification jobs. Each checks out the exact event
  SHA with full history and grants `contents: write` for publication.
  Snapshot publication has produced the linked prerelease; the stable publication
  path has not yet been verified by an actual stable release.
- **Release artifacts**: The workflows and [`.goreleaser.yaml`](.goreleaser.yaml)
  use GoReleaser `v2.18.3` and UPX `v5.2.1` (installed via
  `jaxxstorm/action-install-gh-release`) to build six CGO-disabled server archives
  (`krita-mcp_<VERSION>_<OS>_<ARCH>.tar.zst` for `darwin`, `linux`, and `windows` on
  `amd64` and `arm64`), the Krita importer archive `krita-mcp-plugin_<VERSION>.zip`
  (with only the archive copy of `kritamcp/bridge.py` stamped to `<VERSION>`),
  and `SHA256SUMS`. The plugin ZIP is staged outside `dist` while GoReleaser cleans
  and builds; GoReleaser generates checksums for all seven archives.
- **Compression**: GoReleaser's native `tar.zst` archiving is used directly without
  custom compression helpers or custom zstd level/thread overrides. Native UPX
  `compress: best` runs on `linux` and `windows` (`amd64` and `arm64`),
  while `darwin` binaries are left unpacked because UPX does not support modern macOS.
  The plugin `.zip` is the sole `.zip` asset because Krita's **Import Python Plugin**
  action expects a `.zip` archive.
- **Local GoReleaser check**: `.goreleaser.yaml` reads `RELEASE_VERSION` and
  `PLUGIN_ZIP` from the environment and requires `upx` on `PATH` (with `UPX` unset)
  when running `goreleaser check` or `goreleaser release --snapshot --clean --skip=publish`.

## Verification and Credits

Run the Go server tests and static checks:

```sh
go test ./...
go test -race ./...
go vet ./...
```

Run the plugin Python unit tests (Python 3.11+):

```sh
python3 -B -m unittest discover -s tests -p "test_*.py" -v
```

`tests/test_bridge.py` and the pure-geometry tests in `tests/test_rendering.py`
run with the Python standard library alone. Qt-dependent tests in
`tests/test_rendering.py` and `tests/test_lifecycle.py` skip automatically when
`PyQt5` is unavailable; set `KRITA_MCP_TEST_PYTHON` (plus optional
`KRITA_MCP_TEST_PYTHONHOME` and `KRITA_MCP_TEST_FRAMEWORK_PATH`) to run
`tests/test_lifecycle.py` against a real Qt/Krita Python runtime, and provide
`PyQt5` (plus `PyKrita` for `tests/test_preview.py`) on `PYTHONPATH` for
`tests/test_commands.py` and `tests/test_preview.py`.

Unit tests and CI cross-builds do not substitute for live Krita QA across target
operating systems. Isolated native functional tests on macOS Krita 5.3.4.1 pass,
but isolated Krita exits with `SIGSEGV` (`-11`); that shutdown crash remains
unresolved. Crash-free shutdown, live plugin installation/importer GUI workflows,
and native Windows/Linux runtime execution are not verified.

Architecture and painting-workflow ideas were inspired by MIT-licensed
[edithatogo/krita-cli](https://github.com/edithatogo/krita-cli); this remains an MCP
bridge, not a CLI port. Release workflow design was inspired by
[teamsunset/LavaFishing](https://github.com/teamsunset/LavaFishing). MCP protocol
handling uses [`github.com/modelcontextprotocol/go-sdk`](https://github.com/modelcontextprotocol/go-sdk)
(`v1.8.0`). This project's [LICENSE](LICENSE) remains unchanged (MIT).
