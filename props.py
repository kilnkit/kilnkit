import bpy

# ================================================================
# Constants & defaults
# ================================================================

ADDON_VERSION = "v1.0.2"

PBR_RULES_DEFAULT = {
    'basecolor': '_c',
    'normal':    '_n',
    'roughness': '_r',
    'height':    '_h',
    'ao':        '_ao',
    'metallic':  '_m',
}

# Filename rule presets
PBR_PRESETS = {
    'CUSTOM': {
        'label': "Custom",
        'rules': None,
    },
    'SUBSTANCE': {
        'label': "Substance Painter",
        'rules': {
            'basecolor': '_BaseColor',
            'normal':    '_Normal',
            'roughness': '_Roughness',
            'height':    '_Height',
            'ao':        '_AmbientOcclusion',
            'metallic':  '_Metallic',
        },
    },
    'UNREAL': {
        'label': "Unreal Engine",
        'rules': {
            'basecolor': '_BC',
            'normal':    '_N',
            'roughness': '_R',
            'height':    '_H',
            'ao':        '_AO',
            'metallic':  '_M',
        },
    },
    'UNITY': {
        'label': "Unity (URP)",
        'rules': {
            'basecolor': '_BaseMap',
            'normal':    '_BumpMap',
            'roughness': '_SpecGlossMap',
            'height':    '_ParallaxMap',
            'ao':        '_OcclusionMap',
            'metallic':  '_MetallicGlossMap',
        },
    },
    'POLYHAVEN': {
        'label': "Poly Haven",
        'rules': {
            'basecolor': '_diff',
            'normal':    '_nor_gl',
            'roughness': '_rough',
            'height':    '_disp',
            'ao':        '_ao',
            'metallic':  '_metal',
        },
    },
}

# Per-channel keywords (synonyms) for AUTO detection — filename tokens are matched
# from the end, suffix-independent. len>=4 substring match, len<=3 exact token match
# (avoids false positives on short abbreviations).
PBR_KEYWORDS = {
    'basecolor': ('basecolor', 'albedo', 'diffuse', 'diff', 'colour', 'color'),
    'normal':    ('normal', 'norm', 'nrm', 'nor'),
    'roughness': ('roughness', 'rough', 'rgh'),
    'metallic':  ('metalness', 'metallic', 'metal', 'mtl'),
    'height':    ('displacement', 'displace', 'disp', 'heightmap', 'height', 'bump'),
    'ao':        ('ambientocclusion', 'occlusion', 'occ', 'ao'),
}

SUPPORTED_EXTS    = ('.png', '.jpg', '.jpeg', '.exr', '.tif', '.tiff')
DEFAULT_UV_ANGLE  = 45.0
DEFAULT_PACK_MARGIN = 0.005
DEFAULT_DECIMATE  = 0.5
DEFAULT_UV_SCALE  = 1.0
DEFAULT_AO        = 1.0
DEFAULT_NORMAL    = 1.0
DEFAULT_HEIGHT    = 0.01
DEFAULT_TRIPLANAR_BLEND = 0.3   # triplanar (Box) face-edge blending
DEFAULT_WORLD_STRENGTH = 1.0    # default render environment (world) light strength

# Two independent axes of uv_method — never conflate them.
# UNWRAP: rewrites the mesh's UV layer. Everything else leaves UVs untouched.
# OBJECT_COORD: the node graph samples Object coordinates, so any UV layer is ignored.
# KEEP is in neither set: it unwraps nothing and samples the existing UV layer.
UV_UNWRAP_METHODS     = frozenset({'UV', 'CUBE', 'SLIM'})
UV_OBJECT_COORD_METHODS = frozenset({'OBJECT', 'TRIPLANAR'})

# Library materials have no mesh, so they must not sample UV coordinates — the target mesh
# is unknown at build time and may have no UV map. Coordinate-based mapping always works.
LIBRARY_UV_METHOD = 'TRIPLANAR'

LIBRARY_SUBDIR = "Kilnkit"     # add-on output subfolder inside the User Library (single place to change)

# Identifier names for datablocks created by the render-output module — re-runs
# find and replace by these names (idempotent).
KK_WORLD_NAME   = "KK_World"          # add-on's dedicated world
KK_WORLD_BG     = "KK_World_BG"       # world Background node
KK_WORLD_ENV    = "KK_World_Env"      # world Environment Texture node (HDRI)
KK_LIGHTS_COLL  = "KK_Render_Lights"  # studio 3-point light collection
KK_TURNTABLE_PIVOT = "KK_Turntable_Pivot"  # camera-orbit pivot empty for turntable renders


# ================================================================
# Helpers
# ================================================================

def fn_get_rules():
    """Return the currently effective suffix rules — Preferences first, defaults when empty."""
    prefs = bpy.context.preferences.addons.get(__package__)
    if not prefs:
        return PBR_RULES_DEFAULT
    p = prefs.preferences

    # When a preset is selected, return its rules
    preset = p.suffix_preset
    if preset == 'AUTO':
        return None   # keyword auto-detection — fn_scan_textures routes to fn_detect_keyword
    if preset != 'CUSTOM' and preset in PBR_PRESETS:
        return PBR_PRESETS[preset]['rules']

    # Custom: each field wins, defaults when empty
    rules = {}
    for key, default in PBR_RULES_DEFAULT.items():
        val = getattr(p, f"suffix_{key}", "").strip()
        rules[key] = val if val else default
    return rules


# ================================================================
# Addon Preferences (persisted)
# ================================================================

def _preset_update(self, context):
    """Auto-fill the suffix fields when the preset changes (AUTO/CUSTOM leave fields untouched)."""
    preset = self.suffix_preset
    if preset not in PBR_PRESETS or PBR_PRESETS[preset]['rules'] is None:
        return
    rules = PBR_PRESETS[preset]['rules']
    for key, val in rules.items():
        setattr(self, f"suffix_{key}", val)


class KILNKIT_Preferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    suffix_preset: bpy.props.EnumProperty(
        name="Preset",
        items=[
            ('AUTO',      "Auto Detect",       "Detect map types from file names — works with any suffix (recommended)"),
            ('CUSTOM',    "Custom",            "Enter suffixes manually"),
            ('SUBSTANCE', "Substance Painter", "Substance Painter default naming"),
            ('UNREAL',    "Unreal Engine",     "Unreal Engine naming"),
            ('UNITY',     "Unity (URP)",       "Unity URP naming"),
            ('POLYHAVEN', "Poly Haven",        "Poly Haven CC0 texture naming"),
        ],
        default='AUTO',
        update=_preset_update,
    )

    suffix_basecolor: bpy.props.StringProperty(name="Base Color",  default='_c')
    suffix_normal:    bpy.props.StringProperty(name="Normal",       default='_n')
    suffix_roughness: bpy.props.StringProperty(name="Roughness",    default='_r')
    suffix_height:    bpy.props.StringProperty(name="Height",       default='_h')
    suffix_ao:        bpy.props.StringProperty(name="AO",           default='_ao')
    suffix_metallic:  bpy.props.StringProperty(name="Metallic",     default='_m')

    def draw(self, context):
        l = self.layout
        l.label(text="Texture Filename Suffix Rules", icon='FILE_TEXT')
        l.separator()
        l.prop(self, "suffix_preset")
        if self.suffix_preset == 'AUTO':
            l.label(text="Auto-detected from file names — the suffixes below are ignored", icon='INFO')
        l.separator()
        grid = l.grid_flow(row_major=True, columns=2, even_columns=True)
        grid.prop(self, "suffix_basecolor")
        grid.prop(self, "suffix_normal")
        grid.prop(self, "suffix_roughness")
        grid.prop(self, "suffix_height")
        grid.prop(self, "suffix_ao")
        grid.prop(self, "suffix_metallic")
        l.separator()
        l.operator("kilnkit.reset_suffix", text="Reset Custom Defaults", icon='LOOP_BACK')


# ================================================================
# SlotEntry — per-object slots
# ================================================================

# Flag that blocks the _update_* callbacks from writing back to nodes while
# auto-sync is updating the sliders
_syncing = False


def _write_node_value(entry, context, node_name, input_idx, value):
    """On slider change, update only the matching node input — other nodes untouched."""
    obj = context.active_object
    if not obj:
        return
    for i, e in enumerate(obj.kilnkit_slots):
        if e == entry:
            if i < len(obj.data.materials) and obj.data.materials[i]:
                mat = obj.data.materials[i]
                if mat.node_tree and node_name in mat.node_tree.nodes:
                    mat.node_tree.nodes[node_name].inputs[input_idx].default_value = value
            break


def _read_node_value(entry, context, node_name, input_idx):
    """Read and return one node input value — mirror of _write_node_value. None when absent."""
    obj = context.active_object
    if not obj:
        return None
    for i, e in enumerate(obj.kilnkit_slots):
        if e == entry:
            if i < len(obj.data.materials) and obj.data.materials[i]:
                mat = obj.data.materials[i]
                if mat.node_tree and node_name in mat.node_tree.nodes:
                    return mat.node_tree.nodes[node_name].inputs[input_idx].default_value
            return None
    return None


def _read_triplanar_blend(entry, context):
    """Read the BOX projection blend — Base Color (KK_BaseColor) map first, else the first
       BOX node, else None. Blend can differ per texture, so the reference is pinned to
       Base Color (writing via the slider updates every BOX node together).
       projection_blend is a node attribute, not a socket, so it can't live in the
       socket-based _SYNC_MAP and is handled separately."""
    obj = context.active_object
    if not obj:
        return None
    for i, e in enumerate(obj.kilnkit_slots):
        if e == entry:
            if i < len(obj.data.materials) and obj.data.materials[i]:
                mat = obj.data.materials[i]
                if mat.node_tree:
                    nodes = mat.node_tree.nodes
                    bc = nodes.get("KK_BaseColor")
                    if bc and bc.type == 'TEX_IMAGE' and bc.projection == 'BOX':
                        return bc.projection_blend
                    for n in nodes:
                        if n.type == 'TEX_IMAGE' and n.projection == 'BOX':
                            return n.projection_blend
            return None
    return None


def _update_uv_scale(self, context):
    if _syncing:
        return
    _write_node_value(self, context, "KK_Mapping", 3, tuple(self.uv_scale))

def _get_uv_uniform(self):
    return self.uv_scale[0]

def _set_uv_uniform(self, value):
    # Uniform slider — same value on all three axes (assigning uv_scale fires _update_uv_scale → writes nodes)
    self.uv_scale = (value, value, value)

def _update_uv_scale_split(self, context):
    # Collapsing to uniform mode unifies all axes to X (assignment fires _update_uv_scale → writes nodes)
    if not self.uv_scale_split:
        x = self.uv_scale[0]
        self.uv_scale = (x, x, x)

def _update_ao_strength(self, context):
    if _syncing:
        return
    _write_node_value(self, context, "KK_AO_Mix", 0, self.ao_strength)

def _update_normal_strength(self, context):
    if _syncing:
        return
    _write_node_value(self, context, "KK_NormalMap", 0, self.normal_strength)

def _update_height_scale(self, context):
    if _syncing:
        return
    _write_node_value(self, context, "KK_Displacement", 2, self.height_scale)

def _update_triplanar_blend(self, context):
    """Triplanar edge blend — update projection_blend on that material's BOX texture nodes only."""
    if _syncing:
        return
    obj = context.active_object
    if not obj:
        return
    for i, e in enumerate(obj.kilnkit_slots):
        if e == self:
            if i < len(obj.data.materials) and obj.data.materials[i]:
                mat = obj.data.materials[i]
                if mat.node_tree:
                    for n in mat.node_tree.nodes:
                        if n.type == 'TEX_IMAGE' and n.projection == 'BOX':
                            n.projection_blend = self.triplanar_blend
            break


def _update_world_strength(self, context):
    """Environment strength slider → live update of the KK_World Background Strength (when present)."""
    w = bpy.data.worlds.get(KK_WORLD_NAME)
    if w and w.node_tree:
        bg = w.node_tree.nodes.get(KK_WORLD_BG)
        if bg:
            bg.inputs["Strength"].default_value = self.world_strength


def _update_world_color(self, context):
    """Flat environment color → live update of the KK_World Background Color (Flat preset only)."""
    if self.render_light_preset != 'FLAT':
        return
    w = bpy.data.worlds.get(KK_WORLD_NAME)
    if w and w.node_tree:
        bg = w.node_tree.nodes.get(KK_WORLD_BG)
        if bg and not bg.inputs["Color"].links:   # leave untouched when an HDRI texture is linked
            c = self.world_color
            bg.inputs["Color"].default_value = (c[0], c[1], c[2], 1.0)


def _get_camera_lens(self):
    # Live proxy: when KK_Camera exists the slider IS the camera's focal length, so it stays
    # in sync with Properties > Camera both ways. No camera yet → the remembered value used
    # as the next placement's default.
    cam = bpy.data.objects.get("KK_Camera")
    if cam and cam.type == 'CAMERA':
        return cam.data.lens
    return self.get("_camera_lens_store", 50.0)


def _set_camera_lens(self, value):
    self["_camera_lens_store"] = value             # remember for the next placement
    cam = bpy.data.objects.get("KK_Camera")
    if cam and cam.type == 'CAMERA':
        cam.data.lens = value                      # live-adjust the placed camera


# (slider attribute, node name, input index, tolerance) — scalar channels only
# (the uv_scale vector is handled separately below)
_SYNC_MAP = (
    ("ao_strength",     "KK_AO_Mix",       0, 0.0005),
    ("normal_strength", "KK_NormalMap",    0, 0.0005),
    ("height_scale",    "KK_Displacement", 2, 0.00005),
)


def sync_active_slot_from_nodes(context):
    """Read the active slot's node values and update only the sliders that drifted.

    - Channels with no node are silently skipped.
    - uv_scale syncs all three axes as-is (per-axis values preserved); when non-uniform,
      uv_scale_split is enabled so the panel expands.
    - While updating sliders, the _syncing flag blocks the _update_* callbacks from
      writing back to the nodes.
    """
    global _syncing
    obj = context.active_object
    if not obj or obj.type != 'MESH' or not obj.kilnkit_slots:
        return
    sp = context.scene.kilnkit_scene_props
    idx = sp.active_slot_index
    if not (0 <= idx < len(obj.kilnkit_slots)):
        return
    entry = obj.kilnkit_slots[idx]

    pending = []
    for attr, node_name, input_idx, tol in _SYNC_MAP:
        raw = _read_node_value(entry, context, node_name, input_idx)
        if raw is None:
            continue
        if abs(raw - getattr(entry, attr)) > tol:
            pending.append((attr, raw))

    # uv_scale (vector) — sync all three KK_Mapping Scale axes. The old "skip when
    # non-uniform" rule is gone: per-axis values are preserved
    raw = _read_node_value(entry, context, "KK_Mapping", 3)
    if raw is not None:
        cur = entry.uv_scale
        if any(abs(raw[k] - cur[k]) > 0.0005 for k in range(3)):
            pending.append(("uv_scale", (raw[0], raw[1], raw[2])))
            # Auto-expand the panel when non-uniform (the uniform slider alone shows only X — misleading)
            if abs(raw[0] - raw[1]) > 0.0005 or abs(raw[0] - raw[2]) > 0.0005:
                pending.append(("uv_scale_split", True))

    # triplanar_blend — a node attribute (not a socket), so it is read separately and
    # joins the same pending mechanism
    blend = _read_triplanar_blend(entry, context)
    if blend is not None and abs(blend - entry.triplanar_blend) > 0.0005:
        pending.append(("triplanar_blend", blend))

    if not pending:
        return
    _syncing = True
    try:
        for attr, value in pending:
            setattr(entry, attr, value)
    finally:
        _syncing = False


class KILNKIT_SlotEntry(bpy.types.PropertyGroup):
    directory:       bpy.props.StringProperty(name="Texture Folder", subtype='DIR_PATH')
    # uv_scale mirrors KK_Mapping Scale (X/Y/Z). The float→vector switch (0.7.x) resets the
    # slider to defaults in older .blend files, but nodes are the source of truth →
    # recovered via "Pull Node Values" / auto-sync.
    uv_scale:        bpy.props.FloatVectorProperty(name="Texture Scale", size=3, subtype='XYZ', min=0.001, soft_max=20.0, max=100.0, default=(DEFAULT_UV_SCALE,) * 3, step=1, precision=3, update=_update_uv_scale)
    uv_scale_uniform: bpy.props.FloatProperty(name="Texture Scale", min=0.001, soft_max=20.0, max=100.0, step=1, precision=3, get=_get_uv_uniform, set=_set_uv_uniform, description="Uniform scale for all three axes — expand with ▶ to adjust each axis")
    uv_scale_split:  bpy.props.BoolProperty(name="Split X/Y/Z", default=False, update=_update_uv_scale_split, description="Adjust texture scale per axis. Collapsing unifies all axes to the X value")
    ao_strength:     bpy.props.FloatProperty(name="AO Strength", min=0.0,   max=1.0,                  default=DEFAULT_AO,       step=1, precision=3, update=_update_ao_strength)
    normal_strength: bpy.props.FloatProperty(name="Normal",      min=0.0,   soft_max=2.0, max=10.0,   default=DEFAULT_NORMAL,   step=1, precision=3, update=_update_normal_strength)
    height_scale:    bpy.props.FloatProperty(name="Height",      min=0.0,   soft_max=0.2, max=1.0,    default=DEFAULT_HEIGHT,   step=1, precision=4, update=_update_height_scale)
    triplanar_blend: bpy.props.FloatProperty(name="Edge Blend", min=0.0,   max=1.0,                  default=DEFAULT_TRIPLANAR_BLEND, step=1, precision=3, update=_update_triplanar_blend, description="How strongly the three triplanar projections blend at their seams — higher is smoother. Dragging updates every map; syncing from the Shader Editor reads the Base Color map's Blend value")


# ================================================================
# SceneProps — scene-wide settings
# ================================================================

def _active_slot_update(self, context):
    """Selecting a slot also sets Blender's active material slot — keeps the Shader Editor and Properties in sync."""
    obj = context.active_object
    if obj and obj.type == 'MESH' and 0 <= self.active_slot_index < len(obj.data.materials):
        obj.active_material_index = self.active_slot_index


class KILNKIT_SceneProps(bpy.types.PropertyGroup):

    active_tab: bpy.props.EnumProperty(
        items=[
            ('MAIN',     "Main",     "Manage material slots"),
            ('SETTINGS', "Settings", "Pipeline settings"),
            ('BATCH',    "Batch",    "Run on multiple objects"),
            ('LIBRARY',  "Library",  "Build materials from folders without a mesh and register them as assets"),
            ('RENDER',   "Render",   "Automate environment, lighting, camera, and render output"),
        ],
        default='MAIN'
    )

    # Library tab — staging path for folder→material builds (no mesh needed)
    library_dir: bpy.props.StringProperty(name="Texture Folder", subtype='DIR_PATH')

    active_slot_index: bpy.props.IntProperty(default=0, update=_active_slot_update)
    library_active_index: bpy.props.IntProperty(default=0)

    # Pipeline steps
    step_scale: bpy.props.BoolProperty(name="Apply Scale", default=True)
    step_uv:    bpy.props.BoolProperty(name="Smart UV",    default=True)
    step_pbr:   bpy.props.BoolProperty(name="Build PBR Nodes", default=True)

    # Material handling
    mat_conflict: bpy.props.EnumProperty(
        name="Existing Material",
        items=[
            ('OVERWRITE', "Overwrite", "Reset the existing material's nodes and rewire"),
            ('DUPLICATE', "Duplicate", "Create a new material with a .001 suffix"),
        ],
        default='OVERWRITE'
    )

    # UV settings
    uv_method: bpy.props.EnumProperty(
        name="UV Mapping Method",
        items=[
            # (identifier, label, description [hover tooltip], int value) — int values are
            # pinned for compatibility with existing .blend files
            ('TRIPLANAR', "Triplanar", "For tiling textures — three-axis projection with no seams, uniform on any shape. Preview default: no UV map is created, so baking and engine export need a Finish step", 3),
            ('KEEP',      "Keep Existing UV", "Use the UV map the mesh already has — nothing is unwrapped or overwritten. Best for imported assets an artist already unwrapped", 5),
            ('OBJECT',    "Object",    "Seamless textures — flat projection in Object coordinates. Sides and back may stretch", 2),
            ('CUBE',      "Cube",      "Box-shaped objects — six-sided box projection. Creates a UV map", 1),
            ('UV',        "Smart UV",  "Baking / unique textures — auto seams by angle, then unwrap. Overwrites the existing UV map", 0),
            ('SLIM',      "SLIM Unwrap", "High-quality unwrap — auto seams + minimum stretch. Overwrites the existing UV map", 4),
        ],
        default='TRIPLANAR',
    )
    uv_angle_limit: bpy.props.FloatProperty(
        name="Auto Seam Angle",
        min=0.0, max=89.0, default=DEFAULT_UV_ANGLE,
        description="Shared by Smart UV and SLIM — lower makes more islands with less distortion, higher makes fewer islands with more distortion"
    )
    use_pack_islands: bpy.props.BoolProperty(
        name="Pack Islands",
        default=True,
        description="Pack UV islands tightly after unwrapping"
    )
    pack_rotate: bpy.props.BoolProperty(name="Allow Rotation", default=True)
    pack_margin: bpy.props.FloatProperty(
        name="Island Margin",
        min=0.0, max=0.1, default=DEFAULT_PACK_MARGIN,
        description="Gap between UV islands (0.005–0.02 recommended for baking)"
    )

    # Mesh optimization
    show_mesh_opt:  bpy.props.BoolProperty(name="Decimate",      default=False)
    decimate_ratio: bpy.props.FloatProperty(
        name="Poly Reduction",
        min=0.01, max=1.0, default=DEFAULT_DECIMATE,
        description="1.0 = original / 0.5 = half"
    )
    lod_count: bpy.props.IntProperty(
        name="LOD Levels", min=2, max=6, default=3,
        description="Number of LOD levels to create (LOD0 = original resolution)"
    )
    lod_step: bpy.props.FloatProperty(
        name="Step Ratio", min=0.1, max=0.9, default=0.5,
        description="Reduction ratio per level — LODn ratio = step^n (0.5 halves each level)"
    )

    # Naming
    naming_base: bpy.props.StringProperty(
        name="Asset Name",
        default="",
        description="Shared name for the object and its materials. Empty uses the folder name (first slot with a folder). Manual input recommended for multi-slot assets"
    )
    naming_use_prefix: bpy.props.BoolProperty(name="Use Prefix", default=False)
    naming_prefix:     bpy.props.StringProperty(name="Prefix", default="")
    naming_rename_mats: bpy.props.BoolProperty(
        name="Rename Materials Too",
        default=True,
        description="Off renames only the object and mesh. On renames materials into the same family — single: <name>, multi: <name>_<slot folder>"
    )

    # Main tab toggle
    show_advanced: bpy.props.BoolProperty(name="Fine Tuning", default=False)

    # Node → slider auto-sync (mirrors direct Shader Editor edits)
    auto_sync: bpy.props.BoolProperty(
        name="Auto Sync",
        default=False,
        description="Mirror node values edited in the Shader Editor back to the sliders (0.5 s polling)"
    )

    # ── Render output — lighting / environment ──
    render_light_preset: bpy.props.EnumProperty(
        name="Environment Preset",
        items=[
            ('STUDIO', "Studio 3-Point", "Neutral backdrop + key/fill/rim lights — crisp product-shot look (add lights with 'Set Up 3-Point Lights')"),
            ('HDRI',   "HDRI Environment", "Realistic lighting and reflections from an HDRI image. Set an .hdr/.exr below"),
            ('FLAT',   "Flat Color",     "Solid background light only — the simplest uniform lighting"),
        ],
        default='STUDIO',
    )
    render_hdri_path: bpy.props.StringProperty(
        name="HDRI File", subtype='FILE_PATH',
        description="HDRI (.hdr/.exr) for environment lighting — used with the HDRI preset"
    )
    world_strength: bpy.props.FloatProperty(
        name="Environment Strength", min=0.0, soft_max=5.0, max=100.0,
        default=DEFAULT_WORLD_STRENGTH, step=10, precision=2,
        update=_update_world_strength,
        description="World (environment) light strength — updates live while dragging"
    )
    world_color: bpy.props.FloatVectorProperty(
        name="Environment Color", subtype='COLOR', size=3, min=0.0, max=1.0,
        default=(0.05, 0.05, 0.06), update=_update_world_color,
        description="Background light color for the Flat Color preset"
    )
    # ── Render output — camera ──
    camera_view: bpy.props.EnumProperty(
        name="Camera Angle",
        items=[
            # Labels use "Front View" etc. — Blender's core catalog translates plain
            # "Front"/"Side"/"Top" first (core wins over add-on dictionaries), so the
            # msgids must be strings the core does not own
            ('THREEQ', "3/4 View",   "High diagonal front — the product-shot standard"),
            ('FRONT',  "Front View", "Front, slightly above"),
            ('SIDE',   "Side View",  "Side, slightly above"),
            ('TOP',    "Top View",   "Looking straight down"),
        ],
        default='THREEQ',
    )
    camera_margin: bpy.props.FloatProperty(
        name="Margin", min=1.0, soft_max=2.0, max=3.0, default=1.25, step=5, precision=2,
        description="Space around the asset when placing — the asset spans 1/margin of the frame's tighter side, so 1.25 fills 80%. Larger moves the camera farther away"
    )
    camera_lens: bpy.props.FloatProperty(
        name="Lens (mm)", min=10.0, max=200.0, step=100, precision=0,
        description="Camera focal length — longer is telephoto (less distortion), shorter is wide-angle. Live-synced with the placed camera (Properties > Camera)",
        get=_get_camera_lens, set=_set_camera_lens,
    )

    # ── Render output — settings / output ──
    render_engine_choice: bpy.props.EnumProperty(
        name="Engine",
        items=[
            ('EEVEE',  "EEVEE (Fast)",    "Real-time rasterizer — fast previews and output (recommended default)"),
            ('CYCLES', "Cycles (Quality)", "Path tracing — realistic but slower"),
        ],
        default='EEVEE',
    )
    render_res: bpy.props.EnumProperty(
        name="Resolution",
        items=[('512', "512", "512×512"), ('1024', "1K", "1024×1024"),
               ('2048', "2K", "2048×2048"), ('4096', "4K", "4096×4096")],
        default='1024',
    )
    render_samples: bpy.props.IntProperty(
        name="Render Samples", min=1, soft_max=1024, max=4096, default=512,
        description="Final render sample count — light default of 512 (plenty for visual checks). The viewport automatically uses fewer"
    )
    # Output folder is delegated to the native scene.render.filepath (shown directly in the
    # render tab) — no separate property, so Kilnkit and F12 never disagree.
    render_exist_mode: bpy.props.EnumProperty(
        name="If File Exists",
        items=[
            ('NUMBER',    "Auto Number", "Create a new file as _001, _002… when the name exists (default — prevents overwriting)"),
            ('OVERWRITE', "Overwrite",   "Replace the existing file"),
            ('SKIP',      "Skip",        "Skip rendering when the file already exists"),
        ],
        default='NUMBER',
        description="What to do when the file name already exists (numbers are an _NNN take counter, unlike frame numbers)"
    )

    # ── Render output — turntable (360° camera orbit) ──
    turntable_frames: bpy.props.IntProperty(
        name="Frames", min=12, soft_max=120, max=240, default=60,
        description="Frames for one full 360° turn — more is smoother but slower to render (60 @ 24fps ≈ 2.5 s loop)"
    )
    turntable_fps: bpy.props.IntProperty(
        name="FPS", min=12, soft_max=30, max=60, default=24,
        description="Playback frame rate of the turntable video"
    )
    turntable_format: bpy.props.EnumProperty(
        name="Format",
        items=[
            ('MP4',          "MP4 Video",    "A single .mp4 video (H.264) — easiest to share and upload"),
            ('PNG_SEQUENCE', "PNG Sequence", "One PNG per frame (_0001, _0002…) — for compositing or a transparent background"),
        ],
        default='MP4',
        description="Turntable output — a single video file or a numbered PNG sequence"
    )

    # Render tab section toggles
    show_render_env:    bpy.props.BoolProperty(name="Environment / Background", default=True)
    show_render_light:  bpy.props.BoolProperty(name="Lighting",   default=True)
    show_render_camera: bpy.props.BoolProperty(name="Camera",     default=True)
    show_render_output: bpy.props.BoolProperty(name="Render Settings & Output", default=True)

    # Settings tab section toggles
    show_pipeline: bpy.props.BoolProperty(name="One-Click Steps", default=True)
    show_uv:       bpy.props.BoolProperty(name="UV Settings",     default=True)
    show_material: bpy.props.BoolProperty(name="Material Settings", default=False)
    show_suffix:   bpy.props.BoolProperty(name="Filename Rules",  default=False)
    show_step:     bpy.props.BoolProperty(name="Run Steps Individually", default=False)
    show_utils:    bpy.props.BoolProperty(name="Utilities",       default=False)


# ================================================================
# Registration
# ================================================================

classes = (
    KILNKIT_Preferences,
    KILNKIT_SlotEntry,
    KILNKIT_SceneProps,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.kilnkit_scene_props = bpy.props.PointerProperty(type=KILNKIT_SceneProps)
    bpy.types.Object.kilnkit_slots      = bpy.props.CollectionProperty(type=KILNKIT_SlotEntry)


def unregister():
    del bpy.types.Scene.kilnkit_scene_props
    del bpy.types.Object.kilnkit_slots
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
