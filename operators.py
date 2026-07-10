import bpy
import os
import bmesh
import math
import re
import time
import mathutils

from .props import (
    PBR_RULES_DEFAULT, PBR_KEYWORDS, SUPPORTED_EXTS,
    DEFAULT_UV_SCALE, DEFAULT_AO, DEFAULT_NORMAL, DEFAULT_HEIGHT,
    DEFAULT_UV_ANGLE, DEFAULT_PACK_MARGIN, DEFAULT_DECIMATE,
    DEFAULT_TRIPLANAR_BLEND, LIBRARY_SUBDIR, LIBRARY_UV_METHOD,
    UV_UNWRAP_METHODS, UV_OBJECT_COORD_METHODS,
    KK_WORLD_NAME, KK_WORLD_BG, KK_WORLD_ENV, KK_LIGHTS_COLL, KK_TURNTABLE_PIVOT,
    fn_get_rules, sync_active_slot_from_nodes,
)

# Unlike draw() strings, report/status-bar strings are not auto-translated →
# translate them with pgettext at creation time
from bpy.app.translations import pgettext_iface as iface_, pgettext_rpt as rpt_


# ================================================================
# Core logic functions
# ================================================================

def fn_ensure_object_mode():
    obj = bpy.context.active_object
    if obj and obj.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')


def fn_get_view3d():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                for region in area.regions:
                    if region.type == 'WINDOW':
                        return window, area, region
    return None, None, None


def fn_norm_path(p):
    """Normalize a path — for comparisons ignoring Windows case and separators."""
    return os.path.normcase(os.path.normpath(bpy.path.abspath(p)))


def fn_find_object_material_by_src(src_path):
    """Find a mesh-side kilnkit material built from the same source folder (library
       staging materials excluded). Keeps same-folder reapply/batch sharing while never
       mixing materials whose folder names match but paths differ."""
    target = fn_norm_path(src_path)
    for m in bpy.data.materials:
        if m.get("kilnkit_lib"):
            continue
        ms = m.get("kilnkit_src")
        if ms and fn_norm_path(ms) == target:
            return m
    return None


def fn_resolve_material(obj, base_name, slot_index, conflict_mode, src_path):
    """Decide which material the slot uses — identity = source folder path (not name).

    OVERWRITE 1) if this slot's current material has the same source (or no recorded
                 source + matching name), update in place
              2) if another mesh material was built from the same source, share it
                 (preserves batch / same-folder intent)
              3) otherwise create new — name clashes get Blender's .001 suffix
                 (prevents silent overwrites)
    DUPLICATE always creates a new material.
    """
    cur = obj.data.materials[slot_index] if slot_index < len(obj.data.materials) else None
    if conflict_mode == 'DUPLICATE':
        mat = bpy.data.materials.new(name=base_name)
    else:
        mat = None
        if cur is not None:
            cur_src = cur.get("kilnkit_src")
            if cur_src and fn_norm_path(cur_src) == fn_norm_path(src_path):
                mat = cur                                      # same-folder reapply → in place
            elif not cur_src and cur.name == base_name:
                mat = cur                                      # legacy (no source recorded) in-place update
        if mat is None:
            mat = fn_find_object_material_by_src(src_path)     # share same source
        if mat is None:
            mat = bpy.data.materials.new(name=base_name)       # new (name clash gets .001)
    mat["kilnkit_src"] = src_path                                # record source folder = identity
    fn_ensure_nodes(mat)
    while len(obj.data.materials) <= slot_index:
        obj.data.materials.append(None)
    obj.data.materials[slot_index] = mat
    return mat


def fn_apply_scale(obj):
    # With Shape Keys, scaling vertices directly would skew every key except Basis
    if obj.data.shape_keys and len(obj.data.shape_keys.key_blocks) > 1:
        return False
    fn_ensure_object_mode()
    scale = obj.scale.copy()
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    for v in bm.verts:
        v.co.x *= scale.x
        v.co.y *= scale.y
        v.co.z *= scale.z
    bm.to_mesh(obj.data)
    bm.free()
    obj.data.update()
    obj.scale = (1.0, 1.0, 1.0)
    return True


_UV_METHOD_LABELS = {
    'UV': "Smart UV", 'CUBE': "Cube Projection", 'SLIM': "SLIM Unwrap",
    'OBJECT': "Object Mapping", 'TRIPLANAR': "Triplanar", 'KEEP': "Keep Existing UV",
}


def fn_ensure_nodes(datablock):
    """Make sure a material/world has a node tree, across Blender versions.

    4.x: assigning use_nodes builds the tree. 5.0: use_nodes is deprecated — reading always
    returns True, assigning does nothing, and new datablocks already own a tree. 6.0: the
    property is removed entirely, hence the hasattr guard. node_tree is the real signal.
    """
    if datablock.node_tree is None and hasattr(datablock, "use_nodes"):
        datablock.use_nodes = True
    return datablock.node_tree


def fn_mesh_has_uv(obj):
    """True when the mesh carries at least one UV layer."""
    return bool(obj and obj.type == 'MESH' and obj.data.uv_layers)


def fn_material_coord_source(mat):
    """Which coordinate source the material's node graph actually samples.

    Returns 'Object', 'UV', another TexCoord socket name, or None when unknown.
    The node graph — not the scene setting — is the truth: a scene-wide uv_method says
    nothing about a material that was built earlier or belongs to a different object.
    """
    if not mat or not mat.node_tree:
        return None
    for link in mat.node_tree.links:
        if link.from_node.type == 'TEX_COORD' and link.to_node.name == "KK_Mapping":
            return link.from_socket.name
    return None


def fn_material_mesh_users(mat):
    """Mesh objects that use this material — sharing means a rewire hits all of them."""
    if not mat:
        return []
    return [o for o in bpy.data.objects
            if o.type == 'MESH' and any(m is mat for m in o.data.materials)]


def fn_uv_rewire_risk(obj, uv_method):
    """Meshes that would break if the active object's materials are rewired to uv_method.

    Rewiring edits the material, so every object sharing it follows along. Switching to a
    UV-based source strands any co-user without a UV map — its textures collapse to a
    single texel. Returns the names of those meshes (empty when safe).
    """
    if not obj or obj.type != 'MESH' or uv_method in UV_OBJECT_COORD_METHODS:
        return []                      # Object coords need no UV map — never a risk
    at_risk = []
    for mat in obj.data.materials:
        for user in fn_material_mesh_users(mat):
            if user is not obj and not fn_mesh_has_uv(user) and user.name not in at_risk:
                at_risk.append(user.name)
    return at_risk


def fn_uv_status(obj, uv_method, mat=None):
    """Classify what the material actually does with UVs, for the panel's one-line status.

    'BROKEN'    — the graph samples UV coords but the mesh has no UV map: textures collapse
                  to a single texel. Reachable via a shared material or a stale setting.
    'NO_UV'     — coordinate-based and the mesh has no UV map: renders fine, but baking,
                  texture painting and engine export will not work.
    'IGNORED'   — coordinate-based while a UV map exists: that map is not being sampled.
    'KEPT'      — sampling the mesh's existing UV map, untouched.
    'GENERATED' — the add-on unwrapped and wrote the UV map.

    The built material is the truth. uv_method is a scene-wide *setting* — it says nothing
    about a material built earlier, or one shared with another object — so it is only the
    fallback when there is no graph yet, and the tiebreak between KEPT and GENERATED.
    """
    has_uv = fn_mesh_has_uv(obj)
    coord = fn_material_coord_source(mat)
    if coord is None:                  # no material/graph yet → predict from the setting
        coord = 'Object' if uv_method in UV_OBJECT_COORD_METHODS else 'UV'
    if coord != 'UV':
        return 'IGNORED' if has_uv else 'NO_UV'
    if not has_uv:
        return 'BROKEN'
    return 'KEPT' if uv_method == 'KEEP' else 'GENERATED'


def fn_unwrap_would_overwrite(obj, uv_method):
    """True when running this method would rewrite a UV map the mesh already has."""
    return uv_method in UV_UNWRAP_METHODS and fn_mesh_has_uv(obj)


def fn_smart_uv(obj, sp):
    fn_ensure_object_mode()
    window, area, region = fn_get_view3d()
    if not area:
        return False
    with bpy.context.temp_override(window=window, area=area, region=region):
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='SELECT')
        bpy.ops.uv.smart_project('EXEC_DEFAULT',
            angle_limit=math.radians(sp.uv_angle_limit),
            island_margin=0.0)
        if sp.use_pack_islands:
            bpy.ops.uv.pack_islands('EXEC_DEFAULT',
                rotate=sp.pack_rotate,
                margin=sp.pack_margin,
                shape_method='CONCAVE')
        bpy.ops.object.mode_set(mode='OBJECT')
    return True


def fn_slim_uv(obj, sp):
    """Auto seams (angle-based) + SLIM (MINIMUM_STRETCH) unwrap — high quality, minimal stretch."""
    fn_ensure_object_mode()
    window, area, region = fn_get_view3d()
    if not area:
        return False
    with bpy.context.temp_override(window=window, area=area, region=region):
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='SELECT')
        bpy.ops.mesh.mark_seam(clear=True)            # clear existing seams
        bpy.ops.mesh.select_all(action='DESELECT')
        bpy.ops.mesh.edges_select_sharp(sharpness=math.radians(sp.uv_angle_limit))
        bpy.ops.mesh.mark_seam(clear=False)           # auto seams by angle
        bpy.ops.mesh.select_all(action='SELECT')
        bpy.ops.uv.unwrap(method='MINIMUM_STRETCH', margin=sp.pack_margin)
        if sp.use_pack_islands:
            bpy.ops.uv.pack_islands('EXEC_DEFAULT',
                rotate=sp.pack_rotate,
                margin=sp.pack_margin,
                shape_method='CONCAVE')
        bpy.ops.object.mode_set(mode='OBJECT')
    return True


def fn_cube_uv(obj, sp):
    fn_ensure_object_mode()
    window, area, region = fn_get_view3d()
    if not area:
        return False
    with bpy.context.temp_override(window=window, area=area, region=region):
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='SELECT')
        bpy.ops.uv.cube_project('EXEC_DEFAULT',
            cube_size=1.0,
            correct_aspect=True,
            clip_to_bounds=False,
            scale_to_bounds=False)
        if sp.use_pack_islands:
            bpy.ops.uv.pack_islands('EXEC_DEFAULT',
                rotate=sp.pack_rotate,
                margin=sp.pack_margin,
                shape_method='CONCAVE')
        bpy.ops.object.mode_set(mode='OBJECT')
    return True


def fn_detect_keyword(directory):
    """Auto-detect map types from filename tokens (matched from the end) — suffix-independent.
       Synonym-dictionary matching + normal GL preferred. Map types usually sit at the end of
       a filename, so matching from the back avoids false hits when the asset name contains a keyword."""
    dir_abs = os.path.normpath(bpy.path.abspath(directory))
    if not os.path.isdir(dir_abs):
        return {}
    files = [f for f in os.listdir(dir_abs) if f.lower().endswith(SUPPORTED_EXTS)]
    if not files:
        return {}
    kw = sorted(((w, ch) for ch, words in PBR_KEYWORDS.items() for w in words),
                key=lambda x: -len(x[0]))
    found = {}
    dx_normal = None
    for f in sorted(files):
        name = f.lower().rsplit(".", 1)[0]
        tokens = [t for t in re.split(r'[_\-.\s]+', name) if t]
        matched = False
        for tok in reversed(tokens):
            for w, ch in kw:
                hit = (w in tok) if len(w) >= 4 else (tok == w)
                if not hit:
                    continue
                if ch == 'normal' and 'dx' in tok:          # DirectX normal → held back, GL preferred
                    if dx_normal is None:
                        dx_normal = os.path.join(dir_abs, f)
                else:
                    found.setdefault(ch, os.path.join(dir_abs, f))
                matched = True
                break
            if matched:
                break
    if 'normal' not in found and dx_normal:                 # no GL → take DX
        found['normal'] = dx_normal
    return found


# ----------------------------------------------------------------
# MaterialX (.mtlx) sidecar parsing — when present, the source of truth for channel
# mapping (always wins over presets). Follows the shader graph of the .mtlx the author
# shipped in the folder to read exactly "this file is this channel". Python's built-in
# xml only (keeps zero external dependencies). USD deferred (binary .usdc needs pxr).
# ----------------------------------------------------------------

# standard_surface / UsdPreviewSurface input names → our channels
_MTLX_SURFACE_INPUTS = {
    'base_color':         'basecolor',   # standard_surface
    'diffuseColor':       'basecolor',   # UsdPreviewSurface
    'specular_roughness': 'roughness',
    'roughness':          'roughness',
    'metalness':          'metallic',
    'metallic':           'metallic',
    'normal':             'normal',
    'occlusion':          'ao',          # UsdPreviewSurface has an AO input
}


def _mtlx_tag(el):
    """Tag name with the namespace prefix stripped (some .mtlx files include one)."""
    return el.tag.rsplit('}', 1)[-1]


def _mtlx_inputs(node):
    return [c for c in node if _mtlx_tag(c) == 'input']


def _mtlx_abspath(value, base_dir):
    """mtlx file value (relative/absolute) → existing absolute path with a supported extension, else None."""
    value = (value or '').strip()
    if not value:
        return None
    p = value if os.path.isabs(value) else os.path.join(base_dir, value)
    p = os.path.normpath(p)
    if p.lower().endswith(SUPPORTED_EXTS) and os.path.isfile(p):
        return p
    return None


def _mtlx_resolve_file(name, scope_nodes, base_dir, graphs, depth=0):
    """Trace from a node name to the final texture file path (within scope, following
       nodegraph outputs too). Multi-input nodes like multiply/mix follow the first
       connected input (usually the primary color/map input)."""
    if depth > 16 or name not in scope_nodes:
        return None
    node = scope_nodes[name]
    inputs = _mtlx_inputs(node)
    # If this node has a direct file input, that's the texture
    for inp in inputs:
        if inp.get('name') == 'file' and inp.get('value'):
            return _mtlx_abspath(inp.get('value'), base_dir)
    # Otherwise follow the node/graph its inputs point to
    for inp in inputs:
        ref = inp.get('nodename')
        if ref:
            r = _mtlx_resolve_file(ref, scope_nodes, base_dir, graphs, depth + 1)
            if r:
                return r
        ng = inp.get('nodegraph')
        if ng:
            r = _mtlx_resolve_graph_output(ng, inp.get('output'), base_dir, graphs, depth + 1)
            if r:
                return r
    return None


def _mtlx_resolve_graph_output(ng_name, out_name, base_dir, graphs, depth=0):
    g = graphs.get(ng_name)
    if not g:
        return None
    outs = g['outputs']
    start = outs.get(out_name) if out_name else None
    if not start and len(outs) == 1:                       # only one output → use it
        start = next(iter(outs.values()))
    if not start:
        return None
    return _mtlx_resolve_file(start, g['nodes'], base_dir, graphs, depth + 1)


def _mtlx_input_file(inp, scope_nodes, base_dir, graphs):
    """Texture file path that one surface/material <input> points to."""
    nn = inp.get('nodename')
    if nn:
        r = _mtlx_resolve_file(nn, scope_nodes, base_dir, graphs)
        if r:
            return r
    ng = inp.get('nodegraph')
    if ng:
        return _mtlx_resolve_graph_output(ng, inp.get('output'), base_dir, graphs)
    if inp.get('name') == 'file' and inp.get('value'):     # rare: direct file value
        return _mtlx_abspath(inp.get('value'), base_dir)
    return None


def fn_find_sidecar(directory):
    """Return the MaterialX (.mtlx) sidecar path in the folder (None if absent). First by name when multiple."""
    dir_abs = os.path.normpath(bpy.path.abspath(directory))
    if not os.path.isdir(dir_abs):
        return None
    cands = sorted(f for f in os.listdir(dir_abs) if f.lower().endswith('.mtlx'))
    return os.path.join(dir_abs, cands[0]) if cands else None


def fn_parse_sidecar(directory):
    """Parse the folder's .mtlx (if any) and return {channel: absolute path}. {} when absent
       or unresolvable (→ keyword fallback). Channels the graph doesn't cover (often AO/height)
       are supplemented by keyword detection so nothing goes missing."""
    import xml.etree.ElementTree as ET
    sidecar = fn_find_sidecar(directory)
    if not sidecar:
        return {}
    base_dir = os.path.dirname(sidecar)
    try:
        root = ET.parse(sidecar).getroot()
    except (ET.ParseError, OSError):
        return {}

    # Collect document-scope nodes + nodegraphs (inner nodes/outputs)
    doc_nodes, graphs, surfaces, materials = {}, {}, [], []
    for el in list(root):
        t = _mtlx_tag(el)
        nm = el.get('name')
        if t == 'nodegraph' and nm:
            gnodes, gouts = {}, {}
            for ch in list(el):
                cnm = ch.get('name')
                if not cnm:
                    continue
                if _mtlx_tag(ch) == 'output':
                    gouts[cnm] = ch.get('nodename')
                else:
                    gnodes[cnm] = ch
            graphs[nm] = {'nodes': gnodes, 'outputs': gouts}
        elif nm:
            doc_nodes[nm] = el
            typ = el.get('type') or ''
            if t in ('standard_surface', 'open_pbr_surface', 'UsdPreviewSurface') or typ == 'surfaceshader':
                surfaces.append(el)
            if t == 'surfacematerial' or typ == 'material':
                materials.append(el)

    result = {}

    # 1) Surface shader inputs → channels
    for surf in surfaces:
        for inp in _mtlx_inputs(surf):
            ch = _MTLX_SURFACE_INPUTS.get(inp.get('name'))
            if not ch or ch in result:
                continue
            path = _mtlx_input_file(inp, doc_nodes, base_dir, graphs)
            if path:
                result[ch] = path

    # 2) Displacement (surfacematerial → displacementshader) → height
    if 'height' not in result:
        for mat_el in materials:
            for inp in _mtlx_inputs(mat_el):
                if inp.get('name') != 'displacementshader':
                    continue
                path = _mtlx_input_file(inp, doc_nodes, base_dir, graphs)
                if path:
                    result['height'] = path
                    break
            if 'height' in result:
                break

    # 3) Channels the graph didn't cover (often AO) — supplement via keyword detection
    #    (prevents gaps; graph-resolved channels stay authoritative)
    if result and len(result) < 6:
        for ch, p in fn_detect_keyword(directory).items():
            result.setdefault(ch, p)

    return result


def fn_scan_textures(directory):
    dir_abs = os.path.normpath(bpy.path.abspath(directory))
    if not os.path.isdir(dir_abs):
        return {}
    # An author-shipped .mtlx is the source of truth for channel mapping — always wins
    # regardless of preset (AUTO/manual). Manual suffix presets exist for custom naming
    # in folders *without* a sidecar.
    sidecar_result = fn_parse_sidecar(directory)
    if sidecar_result:
        return sidecar_result
    rules = fn_get_rules()
    if rules is None:                                       # AUTO — keyword detection
        return fn_detect_keyword(directory)
    sorted_rules = sorted(rules.items(), key=lambda x: -len(x[1]))
    image_paths = {}
    for filename in os.listdir(dir_abs):
        if not filename.lower().endswith(SUPPORTED_EXTS):
            continue
        fname_lower = filename.lower()
        for channel, suffix in sorted_rules:
            if suffix.lower() in fname_lower and channel not in image_paths:
                image_paths[channel] = os.path.join(dir_abs, filename)
                break
    return image_paths


def fn_load_image(path):
    existing = next(
        (img for img in bpy.data.images
         if img.filepath == path or bpy.path.abspath(img.filepath) == path),
        None
    )
    return existing if existing else bpy.data.images.load(path)


def fn_build_nodes(mat, directory, uv_method='TRIPLANAR'):
    image_paths = fn_scan_textures(directory)
    if not image_paths:
        return False

    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()

    X = {'in': -1200, 'map': -1000, 'tex': -700, 'proc': -200, 'bsdf': 200, 'out': 550}

    coord   = nodes.new('ShaderNodeTexCoord');       coord.location   = (X['in'], 0)
    mapping = nodes.new('ShaderNodeMapping');         mapping.location = (X['map'], 0); mapping.name = "KK_Mapping"
    bsdf    = nodes.new('ShaderNodeBsdfPrincipled'); bsdf.location    = (X['bsdf'], 0)
    out     = nodes.new('ShaderNodeOutputMaterial'); out.location     = (X['out'], 0)
    use_box = (uv_method == 'TRIPLANAR')
    coord_output = 'Object' if uv_method in UV_OBJECT_COORD_METHODS else 'UV'
    links.new(coord.outputs[coord_output], mapping.inputs[0])
    links.new(bsdf.outputs[0], out.inputs[0])

    tex = {}
    y = 700
    for m_type in ['basecolor', 'ao', 'roughness', 'metallic', 'normal', 'height']:
        if m_type not in image_paths:
            continue
        t = nodes.new('ShaderNodeTexImage')
        t.image = fn_load_image(image_paths[m_type])
        t.image.colorspace_settings.name = 'sRGB' if m_type == 'basecolor' else 'Non-Color'
        if use_box:                       # triplanar — 3-axis box projection
            t.projection = 'BOX'
            t.projection_blend = DEFAULT_TRIPLANAR_BLEND
        if m_type == 'basecolor':
            t.name = "KK_BaseColor"   # name tag so the preview can find it fast
        t.location = (X['tex'], y); y -= 280
        tex[m_type] = t
        links.new(mapping.outputs[0], t.inputs[0])

    if 'basecolor' in tex:
        if 'ao' in tex:
            mix = nodes.new('ShaderNodeMix')
            mix.name = "KK_AO_Mix"; mix.blend_type = 'MULTIPLY'; mix.data_type = 'RGBA'
            mix.inputs[0].default_value = DEFAULT_AO
            mix.location = (X['proc'], 300)
            links.new(tex['basecolor'].outputs[0], mix.inputs[6])
            links.new(tex['ao'].outputs[0],        mix.inputs[7])
            links.new(mix.outputs[2], bsdf.inputs['Base Color'])
        else:
            links.new(tex['basecolor'].outputs[0], bsdf.inputs['Base Color'])

    if 'roughness' in tex: links.new(tex['roughness'].outputs[0], bsdf.inputs['Roughness'])
    if 'metallic'  in tex: links.new(tex['metallic'].outputs[0],  bsdf.inputs['Metallic'])
    if 'normal' in tex:
        nm = nodes.new('ShaderNodeNormalMap'); nm.name = "KK_NormalMap"; nm.location = (X['proc'], -50)
        links.new(tex['normal'].outputs[0], nm.inputs[1])
        links.new(nm.outputs[0], bsdf.inputs['Normal'])
    if 'height' in tex:
        disp = nodes.new('ShaderNodeDisplacement'); disp.name = "KK_Displacement"; disp.location = (X['bsdf'], -350)
        disp.inputs[2].default_value = DEFAULT_HEIGHT
        links.new(tex['height'].outputs[0], disp.inputs[0])
        links.new(disp.outputs[0], out.inputs[2])
        mat.displacement_method = 'BOTH'

    return True


def fn_rewire_texcoord(mat, uv_method):
    """Rewire KK_Mapping's TexCoord input and texture projections to match uv_method (no node rebuild)."""
    if not mat or not mat.node_tree:
        return False
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    mapping = nodes.get("KK_Mapping")
    if not mapping:
        return False
    coord = next((n for n in nodes if n.type == 'TEX_COORD'), None)
    if not coord:
        return False
    for link in list(links):
        if link.to_node == mapping and link.to_socket == mapping.inputs[0]:
            links.remove(link)
    coord_output = 'Object' if uv_method in UV_OBJECT_COORD_METHODS else 'UV'
    links.new(coord.outputs[coord_output], mapping.inputs[0])
    # Triplanar → BOX texture projection, everything else → FLAT
    use_box = (uv_method == 'TRIPLANAR')
    for n in nodes:
        if n.type == 'TEX_IMAGE':
            n.projection = 'BOX' if use_box else 'FLAT'
            if use_box:
                n.projection_blend = DEFAULT_TRIPLANAR_BLEND
    return True


def fn_apply_slider_values(mat, uv, ao, normal, height):
    if not mat or not mat.node_tree:
        return
    nodes = mat.node_tree.nodes
    uv_vec = tuple(uv) if hasattr(uv, '__len__') else (uv, uv, uv)   # uv is a 3-axis vector (legacy scalar tolerated)
    if "KK_Mapping"      in nodes: nodes["KK_Mapping"].inputs[3].default_value      = uv_vec
    if "KK_AO_Mix"       in nodes: nodes["KK_AO_Mix"].inputs[0].default_value       = ao
    if "KK_NormalMap"    in nodes: nodes["KK_NormalMap"].inputs[0].default_value    = normal
    if "KK_Displacement" in nodes: nodes["KK_Displacement"].inputs[2].default_value = height


def fn_get_basecolor_image(mat):
    """Return the slot material's Base Color texture image (None if absent).

    Prefers the KK_BaseColor name tag (new materials); otherwise back-traces from the
    BSDF 'Base Color' input (older materials without the tag). Handles the AO Mix
    node in between as well.
    """
    if not mat or not mat.node_tree:
        return None
    nodes = mat.node_tree.nodes
    named = nodes.get("KK_BaseColor")
    if named and getattr(named, "image", None):
        return named.image
    bsdf = next((n for n in nodes if n.type == 'BSDF_PRINCIPLED'), None)
    if not bsdf:
        return None
    sock = bsdf.inputs.get("Base Color")
    if not sock or not sock.is_linked:
        return None
    src = sock.links[0].from_node
    if src.type == 'TEX_IMAGE':
        return src.image
    # Via AO Mix: basecolor is linked to inputs[6] (fn_build_nodes convention)
    if src.name == "KK_AO_Mix" and len(src.inputs) > 6:
        ao_in = src.inputs[6]
        if ao_in.is_linked and ao_in.links[0].from_node.type == 'TEX_IMAGE':
            return ao_in.links[0].from_node.image
    return None


def fn_slot_folder_synced(mat, directory):
    """Whether the material's textures came from the current slot folder.
       True = in sync / False = folder changed (textures from another folder) /
       None = undeterminable (no texture images)."""
    if not mat or not mat.node_tree or not directory:
        return None
    def _norm(p):
        return os.path.normcase(os.path.normpath(bpy.path.abspath(p)))
    target = _norm(directory)
    has_any = False
    for n in mat.node_tree.nodes:
        if n.type == 'TEX_IMAGE' and n.image and n.image.filepath:
            has_any = True
            if _norm(os.path.dirname(n.image.filepath)) == target:
                return True
    return False if has_any else None


def fn_read_slider_values(mat):
    if not mat or not mat.node_tree:
        return (DEFAULT_UV_SCALE,) * 3, DEFAULT_AO, DEFAULT_NORMAL, DEFAULT_HEIGHT
    nodes = mat.node_tree.nodes
    uv     = tuple(nodes["KK_Mapping"].inputs[3].default_value) if "KK_Mapping"     in nodes else (DEFAULT_UV_SCALE,) * 3
    ao     = nodes["KK_AO_Mix"].inputs[0].default_value         if "KK_AO_Mix"      in nodes else DEFAULT_AO
    normal = nodes["KK_NormalMap"].inputs[0].default_value      if "KK_NormalMap"   in nodes else DEFAULT_NORMAL
    height = nodes["KK_Displacement"].inputs[2].default_value   if "KK_Displacement"in nodes else DEFAULT_HEIGHT
    return uv, ao, normal, height


def fn_run_pipeline(obj, slot_index, sp):
    slots = obj.kilnkit_slots
    if slot_index >= len(slots) or not slots[slot_index].directory:
        return False, rpt_("Slot {n}: set a folder first").format(n=slot_index+1)

    # Bail out before fn_resolve_material — a cancelled run must not leave a material behind.
    if sp.uv_method == 'KEEP' and not fn_mesh_has_uv(obj):
        return False, rpt_("Keep Existing UV needs a UV map — this mesh has none")

    directory = slots[slot_index].directory
    dir_path  = os.path.normpath(bpy.path.abspath(directory))
    mat_name  = f"KK_{os.path.basename(dir_path)}"
    mat       = fn_resolve_material(obj, mat_name, slot_index, sp.mat_conflict, directory)
    obj.active_material_index = slot_index

    if sp.step_scale:
        fn_apply_scale(obj)  # returns False with Shape Keys — the calling Operator reports the warning
    if sp.step_uv:
        if sp.uv_method == 'UV':
            uv_ok = fn_smart_uv(obj, sp)
        elif sp.uv_method == 'CUBE':
            uv_ok = fn_cube_uv(obj, sp)
        elif sp.uv_method == 'SLIM':
            uv_ok = fn_slim_uv(obj, sp)
        else:  # TRIPLANAR, OBJECT, KEEP — no unwrap; the node graph picks the coordinate source
            uv_ok = True
        if not uv_ok:
            return False, rpt_("No 3D Viewport found")
    if sp.step_pbr:
        fn_build_nodes(mat, directory, sp.uv_method)
        uv, ao, normal, height = fn_read_slider_values(mat)
        slots[slot_index].uv_scale        = uv
        slots[slot_index].ao_strength     = ao
        slots[slot_index].normal_strength = normal
        slots[slot_index].height_scale    = height
        # Triplanar builds set the node blend to default → align the slot slider too
        # (consistent "reapply = defaults" behavior)
        if sp.uv_method == 'TRIPLANAR':
            slots[slot_index].triplanar_blend = DEFAULT_TRIPLANAR_BLEND

    return True, mat


# ================================================================
# Operators
# ================================================================

# ── Slot management ────────────────────────────────────────────

class KILNKIT_OT_AddSlot(bpy.types.Operator):
    bl_idname = "kilnkit.add_slot"; bl_label = "Add Slot"; bl_options = {'REGISTER', 'UNDO'}
    bl_description = "Add a material slot"

    def execute(self, context):
        obj = context.active_object
        if not obj:
            self.report({'ERROR'}, rpt_("Select an object")); return {'CANCELLED'}
        obj.kilnkit_slots.add()
        context.scene.kilnkit_scene_props.active_slot_index = len(obj.kilnkit_slots) - 1
        return {'FINISHED'}


class _KILNKIT_ProgressiveBuild:
    """Shared skeleton for batch builds — one item per timer tick keeps the UI responsive.
       Subclasses implement _process_one(context, item)→bool and
       _finish_report(context, cancelled)→set, and execute() returns
       self._run_progressive(context, items) after validation/one-off work.
       Headless (background) has no event loop → synchronous loop fallback.
       ESC cancels (processed items are kept)."""
    _timer = None
    _items = None
    _index = 0
    _done = 0
    _progress_label = "Processing"

    def _run_progressive(self, context, items):
        self._items = list(items)
        self._index = 0
        self._done = 0
        if bpy.app.background or context.window is None:
            for it in self._items:
                if self._process_one(context, it):
                    self._done += 1
            return self._finish_report(context, False)
        wm = context.window_manager
        self._timer = wm.event_timer_add(0.15, window=context.window)
        wm.modal_handler_add(self)
        self._set_status(context)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type == 'ESC':
            self._stop(context)
            return self._finish_report(context, True)
        if event.type != 'TIMER':
            return {'PASS_THROUGH'}
        if self._index >= len(self._items):
            self._stop(context)
            return self._finish_report(context, False)
        if self._process_one(context, self._items[self._index]):
            self._done += 1
        self._index += 1
        self._set_status(context)
        return {'PASS_THROUGH'}

    def _set_status(self, context):
        try:
            n = len(self._items or [])
            if self._index < n:
                context.workspace.status_text_set(iface_("{label} {i}/{n}…  (ESC to cancel)").format(
                    label=iface_(self._progress_label), i=self._index + 1, n=n))
            else:
                context.workspace.status_text_set(None)
        except Exception:
            pass

    def _stop(self, context):
        if self._timer is not None:
            try:
                context.window_manager.event_timer_remove(self._timer)
            except Exception:
                pass
            self._timer = None
        try:
            context.workspace.status_text_set(None)
        except Exception:
            pass

    def cancel(self, context):
        self._stop(context)


class KILNKIT_OT_ImportSubfolders(_KILNKIT_ProgressiveBuild, bpy.types.Operator):
    bl_idname = "kilnkit.import_subfolders"; bl_label = "Import Subfolders"; bl_options = {'REGISTER', 'UNDO'}
    bl_description = "Create a slot and material for each first-level subfolder (with textures) of the chosen parent folder. Deeper folders are not read. One at a time (UI stays responsive, ESC to cancel); scale and UV run once"
    directory: bpy.props.StringProperty(subtype='DIR_PATH', options={'SKIP_SAVE'})
    _progress_label = "Importing subfolders"

    def invoke(self, context, event):
        obj = context.active_object
        if not obj or obj.type != 'MESH':
            self.report({'ERROR'}, rpt_("Select a mesh object")); return {'CANCELLED'}
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        obj = context.active_object
        if not obj or obj.type != 'MESH':
            self.report({'ERROR'}, rpt_("Select a mesh object")); return {'CANCELLED'}
        parent = os.path.normpath(bpy.path.abspath(self.directory)) if self.directory else ""
        if not os.path.isdir(parent):
            self.report({'ERROR'}, rpt_("Choose a folder")); return {'CANCELLED'}

        # Collect only subfolders with textures (first level, by name)
        subdirs = [os.path.join(parent, n) for n in sorted(os.listdir(parent))
                   if os.path.isdir(os.path.join(parent, n)) and fn_scan_textures(os.path.join(parent, n))]
        if not subdirs:
            self.report({'WARNING'}, rpt_("No textures found in subfolders")); return {'CANCELLED'}

        # Skip folders already in slots (dedup for auto-batch only — manual duplicates allowed)
        def _norm(p): return os.path.normcase(os.path.normpath(bpy.path.abspath(p)))
        existing = {_norm(s.directory) for s in obj.kilnkit_slots if s.directory}
        self._skipped = sum(1 for d in subdirs if _norm(d) in existing)
        subdirs  = [d for d in subdirs if _norm(d) not in existing]
        if not subdirs:
            self.report({'WARNING'}, rpt_("All folders are already in slots — nothing new to add")); return {'CANCELLED'}

        sp = context.scene.kilnkit_scene_props
        fn_ensure_object_mode()

        # Scale/UV once per object (finished before texture loading)
        if sp.step_scale and not fn_apply_scale(obj):
            self.report({'WARNING'}, rpt_("Shape Keys present — skipped scale apply"))
        if sp.step_uv:
            if sp.uv_method == 'UV':
                fn_smart_uv(obj, sp)
            elif sp.uv_method == 'CUBE':
                fn_cube_uv(obj, sp)
            elif sp.uv_method == 'SLIM':
                fn_slim_uv(obj, sp)
            # TRIPLANAR/OBJECT — coordinate-based; KEEP — reuses the existing UVs. Skip either way.

        # Progressive — one slot+material per folder (spreads heavy texture loads across ticks)
        self._obj = obj
        self._sp = sp
        self._first_index = len(obj.kilnkit_slots)
        return self._run_progressive(context, subdirs)

    def _process_one(self, context, sub):
        obj, sp = self._obj, self._sp
        if not obj:
            return False
        obj.kilnkit_slots.add()
        idx = len(obj.kilnkit_slots) - 1
        obj.kilnkit_slots[idx].directory = sub
        if sp.step_pbr:
            dir_path = os.path.normpath(bpy.path.abspath(sub))
            mat = fn_resolve_material(obj, f"KK_{os.path.basename(dir_path)}", idx, sp.mat_conflict, sub)
            fn_build_nodes(mat, sub, sp.uv_method)
            uv, ao, normal, height = fn_read_slider_values(mat)
            obj.kilnkit_slots[idx].uv_scale        = uv
            obj.kilnkit_slots[idx].ao_strength     = ao
            obj.kilnkit_slots[idx].normal_strength = normal
            obj.kilnkit_slots[idx].height_scale    = height
        return True

    def _finish_report(self, context, cancelled):
        self._sp.active_slot_index = self._first_index
        msg = rpt_("Imported {n} subfolders as slots").format(n=self._done)
        if self._skipped:
            msg += rpt_(" ({n} already present, skipped)").format(n=self._skipped)
        if cancelled:
            msg = rpt_("Cancelled — ") + msg
        self.report({'INFO'}, msg + rpt_(" — use 'Assign' to assign faces"))
        return {'FINISHED'}


class KILNKIT_OT_RemoveSlot(bpy.types.Operator):
    bl_idname = "kilnkit.remove_slot"; bl_label = "Remove Slot"; bl_options = {'REGISTER', 'UNDO'}
    bl_description = "Remove the selected slot together with its Blender material slot"
    slot_index: bpy.props.IntProperty()

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        obj = context.active_object
        if not obj: return {'CANCELLED'}
        if 0 <= self.slot_index < len(obj.kilnkit_slots):
            obj.kilnkit_slots.remove(self.slot_index)
            if self.slot_index < len(obj.data.materials):
                obj.active_material_index = self.slot_index
                bpy.ops.object.material_slot_remove()
            sp = context.scene.kilnkit_scene_props
            sp.active_slot_index = max(0, sp.active_slot_index - 1)
            self.report({'INFO'}, rpt_("Removed slot {n}").format(n=self.slot_index+1))
        return {'FINISHED'}


class KILNKIT_OT_MoveSlot(bpy.types.Operator):
    bl_idname = "kilnkit.move_slot"; bl_label = "Move Slot"; bl_options = {'REGISTER', 'UNDO'}
    bl_description = "Move the slot up/down in the list. Materials and slider values move with it"
    direction: bpy.props.EnumProperty(
        items=[('UP', "Up", ""), ('DOWN', "Down", "")]
    )

    def execute(self, context):
        obj = context.active_object
        if not obj:
            return {'CANCELLED'}
        sp    = context.scene.kilnkit_scene_props
        slots = obj.kilnkit_slots
        idx   = sp.active_slot_index
        n     = len(slots)

        other = idx - 1 if self.direction == 'UP' else idx + 1
        if other < 0 or other >= n:
            return {'CANCELLED'}

        # Swap kilnkit_slots (directory + slider values included)
        slots.move(idx, other)

        # Swap data.materials
        mats = obj.data.materials
        while len(mats) <= max(idx, other):
            mats.append(None)
        mat_idx, mat_other = mats[idx], mats[other]
        mats[idx]   = mat_other
        mats[other] = mat_idx

        sp.active_slot_index = other
        return {'FINISHED'}


# ── PBR execution ──────────────────────────────────────────────

class KILNKIT_OT_OneClick(bpy.types.Operator):
    bl_idname = "kilnkit.one_click"; bl_label = "Apply PBR"; bl_options = {'REGISTER', 'UNDO'}
    bl_description = "Run scale apply → texture mapping → PBR node build in one click. The default mapping is a triplanar preview that creates no UV map. Opens a folder browser if no folder is set"
    slot_index: bpy.props.IntProperty()
    directory:  bpy.props.StringProperty(subtype='DIR_PATH', options={'SKIP_SAVE'})

    def invoke(self, context, event):
        obj = context.active_object
        if not obj or obj.type != 'MESH':
            self.report({'ERROR'}, rpt_("Select a mesh object")); return {'CANCELLED'}
        slots = obj.kilnkit_slots
        # No slots, or the target slot has no folder → folder browser
        # (cancel never calls execute = quiet exit)
        need_folder = (not slots
                       or self.slot_index >= len(slots)
                       or not slots[self.slot_index].directory)
        if need_folder:
            context.window_manager.fileselect_add(self)
            return {'RUNNING_MODAL'}
        return self.execute(context)

    def execute(self, context):
        obj = context.active_object
        if not obj or obj.type != 'MESH':
            self.report({'ERROR'}, rpt_("Select a mesh object")); return {'CANCELLED'}
        sp = context.scene.kilnkit_scene_props

        # Create the slot if missing — data is only created in operators, never in draw
        if not obj.kilnkit_slots:
            obj.kilnkit_slots.add()
            self.slot_index = 0
            sp.active_slot_index = 0
        if self.slot_index >= len(obj.kilnkit_slots):
            self.slot_index = len(obj.kilnkit_slots) - 1

        # Apply the path chosen in the folder browser
        if self.directory:
            obj.kilnkit_slots[self.slot_index].directory = self.directory
            sp.active_slot_index = self.slot_index

        # Still no folder → quiet exit
        if not obj.kilnkit_slots[self.slot_index].directory:
            return {'CANCELLED'}

        fn_ensure_object_mode()
        if sp.step_scale and obj.data.shape_keys and len(obj.data.shape_keys.key_blocks) > 1:
            self.report({'WARNING'}, rpt_("Shape Keys present — skipped scale apply"))
        ok, result = fn_run_pipeline(obj, self.slot_index, sp)
        if not ok:
            self.report({'ERROR'}, result); return {'CANCELLED'}
        self.report({'INFO'}, rpt_("Slot {n}: PBR applied").format(n=self.slot_index+1))
        return {'FINISHED'}


class KILNKIT_OT_RebuildPBR(bpy.types.Operator):
    bl_idname = "kilnkit.rebuild_pbr"; bl_label = "Rebuild PBR Nodes"; bl_options = {'REGISTER', 'UNDO'}
    bl_description = "Rescan the folder textures and rebuild only the PBR nodes. UV unwrap and scale are untouched"
    slot_index: bpy.props.IntProperty()

    def invoke(self, context, event):
        obj = context.active_object
        if obj and self.slot_index < len(obj.data.materials):
            mat = obj.data.materials[self.slot_index]
            if mat and mat.node_tree and mat.node_tree.nodes:
                return context.window_manager.invoke_confirm(self, event)
        return self.execute(context)

    def execute(self, context):
        obj = context.active_object
        if not obj:
            self.report({'ERROR'}, rpt_("Select an object")); return {'CANCELLED'}
        slots = obj.kilnkit_slots
        if self.slot_index >= len(slots) or not slots[self.slot_index].directory:
            self.report({'ERROR'}, rpt_("Set a folder first")); return {'CANCELLED'}
        sp       = context.scene.kilnkit_scene_props
        dir_path = os.path.normpath(bpy.path.abspath(slots[self.slot_index].directory))
        mat      = fn_resolve_material(obj, f"KK_{os.path.basename(dir_path)}", self.slot_index, sp.mat_conflict, slots[self.slot_index].directory)
        fn_build_nodes(mat, slots[self.slot_index].directory, sp.uv_method)
        self.report({'INFO'}, rpt_("PBR nodes rebuilt"))
        return {'FINISHED'}


class _KILNKIT_UVApplyConfirm:
    """Shared invoke for the two operators that change a material's UV mapping.

    Applying a mapping can lose something in two ways: an unwrap overwrites the mesh's UV
    map, and a rewire edits the *material*, so every object sharing it follows along. Ask
    only when something is actually lost, and say exactly what.
    """
    _reasons: list = []

    def _confirm_reasons(self, context, method):
        """(text, is_alert) lines for the dialog. Blender elides a label that overflows the
        dialog width, so each line stays short and the mesh names get their own row."""
        obj = context.active_object
        reasons = []
        if fn_unwrap_would_overwrite(obj, method):
            reasons.append((iface_("The mesh's existing UV map will be replaced"), True))
        risk = fn_uv_rewire_risk(obj, method)
        if risk:
            shown = ", ".join(risk[:3]) + ("…" if len(risk) > 3 else "")
            reasons.append((iface_("Shared material — these meshes have no UV map:"), True))
            reasons.append(("     " + shown, True))
        return reasons

    def invoke(self, context, event):
        self._reasons = self._confirm_reasons(context, self._target_method(context))
        if self._reasons:
            return context.window_manager.invoke_props_dialog(self, width=460)
        return self.execute(context)

    def draw(self, context):
        layout = self.layout
        for text, alert in self._reasons:
            row = layout.row()
            row.alert = alert
            row.label(text=text, icon='ERROR' if text[0] != " " else 'BLANK1')
        layout.separator()
        layout.label(text="Use [Make Single-User] to keep other meshes untouched.", icon='INFO')


class KILNKIT_OT_ReapplyUV(_KILNKIT_UVApplyConfirm, bpy.types.Operator):
    """Apply the mapping method the user picked. Only rewires the TexCoord source and the
    texture projection — nodes the user added by hand survive (unlike Rebuild PBR Nodes,
    which clears the graph)."""
    bl_idname = "kilnkit.reapply_uv"; bl_label = "Reapply UV"; bl_options = {'REGISTER', 'UNDO'}
    bl_description = ("Apply the selected UV mapping method. Unwrap methods re-unwrap the mesh; "
                      "the others only rewire the material. Nodes you added are kept")
    slot_index: bpy.props.IntProperty()

    def _target_method(self, context):
        return context.scene.kilnkit_scene_props.uv_method

    def execute(self, context):
        obj = context.active_object
        if not obj:
            self.report({'ERROR'}, rpt_("Select an object")); return {'CANCELLED'}
        sp = context.scene.kilnkit_scene_props

        scale_msg = ""
        # Only UV/CUBE/SLIM unwrap. KEEP must never fall through to the fn_smart_uv
        # default below — that would overwrite the very UV map it exists to preserve.
        unwraps = sp.uv_method in UV_UNWRAP_METHODS
        if unwraps:
            scale_ok = fn_apply_scale(obj)
            if not scale_ok:
                self.report({'WARNING'}, rpt_("Shape Keys present — skipped scale apply"))
            else:
                scale_msg = rpt_("Scale + ")
            uv_fn = {'CUBE': fn_cube_uv, 'SLIM': fn_slim_uv}.get(sp.uv_method, fn_smart_uv)
            if not uv_fn(obj, sp):
                self.report({'ERROR'}, rpt_("No 3D Viewport found")); return {'CANCELLED'}
        elif sp.uv_method == 'KEEP' and not fn_mesh_has_uv(obj):
            self.report({'ERROR'}, rpt_("Keep Existing UV needs a UV map — this mesh has none"))
            return {'CANCELLED'}

        # uv_method is scene-wide, so every slot's material has to follow it — not just
        # the active slot (a multi-slot object would end up with mixed coordinate sources).
        # Rewiring edits the material, so meshes sharing it are dragged along.
        risk = fn_uv_rewire_risk(obj, sp.uv_method)
        for mat in obj.data.materials:
            fn_rewire_texcoord(mat, sp.uv_method)
        if risk:
            self.report({'WARNING'}, rpt_("Shared material — no UV map on: {names}")
                        .format(names=", ".join(risk[:3])))

        method_name = rpt_(_UV_METHOD_LABELS.get(sp.uv_method, sp.uv_method))
        pack_msg = " + Pack Islands" if sp.use_pack_islands and unwraps else ""
        self.report({'INFO'}, rpt_("UV reapplied ({detail})").format(detail=f"{scale_msg}{method_name}{pack_msg}"))
        return {'FINISHED'}


class KILNKIT_OT_FinishUV(_KILNKIT_UVApplyConfirm, bpy.types.Operator):
    """Second stage of the workflow: one click gives a triplanar preview, this decides
    what the asset actually needs — the mesh's own UVs, or a fresh unwrap."""
    bl_idname = "kilnkit.finish_uv"; bl_label = "Finish UV"; bl_options = {'REGISTER', 'UNDO'}
    bl_description = "Give the asset a real UV map so it can be baked, painted or exported"

    mode: bpy.props.EnumProperty(
        items=[
            ('KEEP',   "Use Existing UV", "Sample the UV map the mesh already has. Nothing is unwrapped or overwritten"),
            ('UNWRAP', "Unwrap Again",    "Unwrap the mesh and write a new UV map. Replaces any existing UV map"),
        ],
        default='KEEP',
    )

    def _target_method(self, context):
        if self.mode == 'KEEP':
            return 'KEEP'
        current = context.scene.kilnkit_scene_props.uv_method
        return current if current in UV_UNWRAP_METHODS else 'SLIM'

    def execute(self, context):
        obj = context.active_object
        if not obj or obj.type != 'MESH':
            self.report({'ERROR'}, rpt_("Select a mesh object")); return {'CANCELLED'}
        sp = context.scene.kilnkit_scene_props

        if self.mode == 'KEEP':
            if not fn_mesh_has_uv(obj):
                self.report({'ERROR'}, rpt_("Keep Existing UV needs a UV map — this mesh has none"))
                return {'CANCELLED'}
            sp.uv_method = 'KEEP'
            msg = rpt_("Now using the existing UV map")
        else:
            # Respect an unwrap method the user already picked; otherwise SLIM (least stretch).
            if sp.uv_method not in UV_UNWRAP_METHODS:
                sp.uv_method = 'SLIM'
            uv_fn = {'CUBE': fn_cube_uv, 'SLIM': fn_slim_uv}.get(sp.uv_method, fn_smart_uv)
            if not uv_fn(obj, sp):
                self.report({'ERROR'}, rpt_("No 3D Viewport found")); return {'CANCELLED'}
            msg = rpt_("UV map created ({m})").format(m=rpt_(_UV_METHOD_LABELS.get(sp.uv_method, sp.uv_method)))

        # Every slot's material must follow the new coordinate source, not just the active one.
        risk = fn_uv_rewire_risk(obj, sp.uv_method)
        for mat in obj.data.materials:
            fn_rewire_texcoord(mat, sp.uv_method)
        if risk:
            self.report({'WARNING'}, rpt_("Shared material — no UV map on: {names}")
                        .format(names=", ".join(risk[:3])))
        self.report({'INFO'}, msg)
        return {'FINISHED'}


# ── Individual steps ───────────────────────────────────────────

class KILNKIT_OT_StepScale(bpy.types.Operator):
    bl_idname = "kilnkit.step_scale"; bl_label = "1. Apply Scale"; bl_options = {'REGISTER', 'UNDO'}
    bl_description = "Reset the object scale to (1, 1, 1). Skipped if Shape Keys exist"
    slot_index: bpy.props.IntProperty()

    def execute(self, context):
        obj = context.active_object
        if not obj:
            self.report({'ERROR'}, rpt_("Select an object")); return {'CANCELLED'}
        if not fn_apply_scale(obj):
            self.report({'WARNING'}, rpt_("Shape Keys present — skipped scale apply")); return {'CANCELLED'}
        self.report({'INFO'}, rpt_("Scale applied"))
        return {'FINISHED'}


class KILNKIT_OT_StepUV(bpy.types.Operator):
    bl_idname = "kilnkit.step_uv"; bl_label = "2. UV Unwrap"; bl_options = {'REGISTER', 'UNDO'}
    bl_description = "Run the UV unwrap with the selected method"
    slot_index: bpy.props.IntProperty()

    def execute(self, context):
        obj = context.active_object
        if not obj:
            self.report({'ERROR'}, rpt_("Select an object")); return {'CANCELLED'}
        sp = context.scene.kilnkit_scene_props
        if sp.uv_method == 'KEEP':
            self.report({'INFO'}, rpt_("Keeping the existing UV map — nothing to unwrap")); return {'FINISHED'}
        if sp.uv_method in UV_OBJECT_COORD_METHODS:
            self.report({'INFO'}, rpt_("Coordinate-based mapping — no unwrap needed")); return {'FINISHED'}
        uv_fn = {'CUBE': fn_cube_uv, 'SLIM': fn_slim_uv}.get(sp.uv_method, fn_smart_uv)
        if not uv_fn(obj, sp):
            self.report({'ERROR'}, rpt_("No 3D Viewport found")); return {'CANCELLED'}
        method_name = {'CUBE': "Cube Projection", 'SLIM': "SLIM Unwrap"}.get(sp.uv_method, "Smart UV")
        self.report({'INFO'}, rpt_("{m} done").format(m=rpt_(method_name)))
        return {'FINISHED'}


class KILNKIT_OT_StepPBR(bpy.types.Operator):
    bl_idname = "kilnkit.step_pbr"; bl_label = "3. Build PBR Nodes"; bl_options = {'REGISTER', 'UNDO'}
    bl_description = "Scan the folder textures and wire up a Principled BSDF"
    slot_index: bpy.props.IntProperty()

    def execute(self, context):
        obj = context.active_object
        if not obj:
            self.report({'ERROR'}, rpt_("Select an object")); return {'CANCELLED'}
        sp    = context.scene.kilnkit_scene_props
        slots = obj.kilnkit_slots
        if self.slot_index >= len(slots) or not slots[self.slot_index].directory:
            self.report({'ERROR'}, rpt_("Set a folder first")); return {'CANCELLED'}
        dir_path = os.path.normpath(bpy.path.abspath(slots[self.slot_index].directory))
        mat = fn_resolve_material(obj, f"KK_{os.path.basename(dir_path)}", self.slot_index, sp.mat_conflict, slots[self.slot_index].directory)
        fn_build_nodes(mat, slots[self.slot_index].directory, sp.uv_method)
        self.report({'INFO'}, rpt_("PBR nodes built"))
        return {'FINISHED'}


# ── Assign ─────────────────────────────────────────────────────

def fn_assign_would_overwrite(obj, slot_index):
    """Object-mode Assign puts ALL faces on slot_index. True when the mesh has 2+ slots and
    at least one face is currently on a different slot — i.e. that face assignment would be
    lost. Used to confirm before the destructive case (single slot / Edit mode never lose)."""
    if not obj or obj.type != 'MESH' or obj.mode != 'OBJECT' or len(obj.data.materials) < 2:
        return False
    polys = obj.data.polygons
    n = len(polys)
    if n == 0:
        return False
    try:
        import numpy as np
        arr = np.empty(n, dtype=np.int32)
        polys.foreach_get("material_index", arr)
        return bool((arr != slot_index).any())
    except Exception:
        return any(p.material_index != slot_index for p in polys)


class KILNKIT_OT_Assign(bpy.types.Operator):
    bl_idname = "kilnkit.assign"; bl_label = "Assign"; bl_options = {'REGISTER', 'UNDO'}
    bl_description = "Assign this slot's material to faces — Edit mode: selected faces / Object mode: all faces"
    slot_index: bpy.props.IntProperty()

    def invoke(self, context, event):
        # Object mode reassigns every face to this slot; on a multi-slot mesh that wipes the
        # other slots' face assignments → confirm first. Single slot / Edit mode run straight.
        if fn_assign_would_overwrite(context.active_object, self.slot_index):
            return context.window_manager.invoke_confirm(
                self, event,
                title=iface_("Assign to all faces?"),
                message=iface_("Multiple slots — this replaces the other slots' face assignments."),
                confirm_text=iface_("Assign All"))
        return self.execute(context)

    def execute(self, context):
        obj = context.active_object
        if not obj or obj.type != 'MESH':
            self.report({'ERROR'}, rpt_("Select a mesh object")); return {'CANCELLED'}
        if self.slot_index >= len(obj.data.materials) or not obj.data.materials[self.slot_index]:
            self.report({'ERROR'}, rpt_("Run Apply PBR first")); return {'CANCELLED'}
        obj.active_material_index = self.slot_index
        if obj.mode == 'EDIT':
            bpy.ops.object.material_slot_assign()
            self.report({'INFO'}, rpt_("Assigned to selected faces"))
        else:
            window, area, region = fn_get_view3d()
            if not area:
                self.report({'ERROR'}, rpt_("No 3D Viewport found")); return {'CANCELLED'}
            with bpy.context.temp_override(window=window, area=area, region=region):
                bpy.ops.object.mode_set(mode='EDIT')
                bpy.ops.mesh.select_all(action='SELECT')
                bpy.ops.object.material_slot_assign()
                bpy.ops.object.mode_set(mode='OBJECT')
            self.report({'INFO'}, rpt_("Assigned to all faces"))
        return {'FINISHED'}


class KILNKIT_OT_ToggleAsset(bpy.types.Operator):
    bl_idname = "kilnkit.toggle_asset"; bl_label = "Mark/Clear Asset"; bl_options = {'REGISTER', 'UNDO'}
    bl_description = "Mark this slot's material as an asset in the Asset Browser (or clear it). Marked materials can be reused by drag-and-drop from other files"
    slot_index: bpy.props.IntProperty()

    def execute(self, context):
        obj = context.active_object
        if (not obj or self.slot_index >= len(obj.data.materials)
                or not obj.data.materials[self.slot_index]):
            self.report({'ERROR'}, rpt_("Run Apply PBR first")); return {'CANCELLED'}
        mat = obj.data.materials[self.slot_index]
        if mat.asset_data:
            mat.asset_clear()
            self.report({'INFO'}, rpt_("Asset cleared: {name}").format(name=mat.name))
        else:
            mat.asset_mark()
            try:
                mat.asset_generate_preview()
            except Exception:
                pass
            self.report({'INFO'}, rpt_("Asset marked: {name} (see the Asset Browser)").format(name=mat.name))
        return {'FINISHED'}


# ── Library (mesh-independent) ─────────────────────────────────

def fn_build_library_material(directory):
    """Folder → PBR material datablock (no mesh needed). None when no textures.
       Reuses (rewires) an existing library material from the same source folder —
       mesh materials are never touched. use_fake_user prevents 0-user deletion +
       asset mark + kilnkit tags.

       Always built with LIBRARY_UV_METHOD (coordinate-based): a library material has no
       mesh, so it must not depend on a UV map that the future target may not have. Using
       the scene's uv_method here would make the result depend on whatever was selected at
       build time."""
    if not fn_scan_textures(directory):
        return None
    dir_norm = os.path.normcase(os.path.normpath(bpy.path.abspath(directory)))
    existing = next(
        (m for m in bpy.data.materials
         if m.get("kilnkit_lib") and m.get("kilnkit_src")
         and os.path.normcase(os.path.normpath(bpy.path.abspath(m["kilnkit_src"]))) == dir_norm),
        None)
    if existing:
        mat = existing
    else:
        base = os.path.basename(os.path.normpath(bpy.path.abspath(directory)))
        mat = bpy.data.materials.new(name=f"KK_{base}")
    fn_ensure_nodes(mat)
    fn_build_nodes(mat, directory, LIBRARY_UV_METHOD)
    mat.use_fake_user = True
    mat["kilnkit_lib"] = True
    mat["kilnkit_src"] = directory
    try:
        mat.asset_mark()
        mat.asset_generate_preview()
    except Exception:
        pass
    return mat


class KILNKIT_OT_LibBuild(bpy.types.Operator):
    bl_idname = "kilnkit.lib_build"; bl_label = "Build Material"; bl_options = {'REGISTER', 'UNDO'}
    bl_description = "Build a PBR material from the folder's textures and add it to the library list (no mesh needed)"
    directory: bpy.props.StringProperty(subtype='DIR_PATH', options={'SKIP_SAVE'})

    def invoke(self, context, event):
        if not context.scene.kilnkit_scene_props.library_dir:
            context.window_manager.fileselect_add(self)
            return {'RUNNING_MODAL'}
        return self.execute(context)

    def execute(self, context):
        sp = context.scene.kilnkit_scene_props
        if self.directory:
            sp.library_dir = self.directory
        if not sp.library_dir:
            self.report({'ERROR'}, rpt_("Set a folder first")); return {'CANCELLED'}
        mat = fn_build_library_material(sp.library_dir)
        if not mat:
            self.report({'WARNING'}, rpt_("No textures found")); return {'CANCELLED'}
        self.report({'INFO'}, rpt_("Library material created: {name}").format(name=mat.name))
        return {'FINISHED'}


class KILNKIT_OT_LibBuildSubfolders(_KILNKIT_ProgressiveBuild, bpy.types.Operator):
    bl_idname = "kilnkit.lib_build_subfolders"; bl_label = "Batch Subfolders"; bl_options = {'REGISTER', 'UNDO'}
    bl_description = "Build a material for each first-level subfolder (with textures) of the chosen parent folder (no mesh needed). One at a time (UI stays responsive, ESC to cancel)"
    directory: bpy.props.StringProperty(subtype='DIR_PATH', options={'SKIP_SAVE'})
    _progress_label = "Building library"

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        sp = context.scene.kilnkit_scene_props
        parent = os.path.normpath(bpy.path.abspath(self.directory)) if self.directory else ""
        if not os.path.isdir(parent):
            self.report({'ERROR'}, rpt_("Choose a folder")); return {'CANCELLED'}
        subdirs = [os.path.join(parent, n) for n in sorted(os.listdir(parent))
                   if os.path.isdir(os.path.join(parent, n)) and fn_scan_textures(os.path.join(parent, n))]
        if not subdirs:
            self.report({'WARNING'}, rpt_("No textures found in subfolders")); return {'CANCELLED'}
        return self._run_progressive(context, subdirs)

    def _process_one(self, context, sub):
        return bool(fn_build_library_material(sub))

    def _finish_report(self, context, cancelled):
        msg = rpt_("Created {n} library materials").format(n=self._done)
        if cancelled:
            msg = rpt_("Cancelled — ") + msg
        self.report({'INFO'}, msg)
        return {'FINISHED'}


class KILNKIT_OT_LibRebuild(bpy.types.Operator):
    bl_idname = "kilnkit.lib_rebuild"; bl_label = "Rewire"; bl_options = {'REGISTER', 'UNDO'}
    bl_description = "Rescan this library material's source folder and rebuild its nodes"
    mat_name: bpy.props.StringProperty()

    def execute(self, context):
        mat = bpy.data.materials.get(self.mat_name)
        if not mat or not mat.get("kilnkit_src"):
            self.report({'ERROR'}, rpt_("Not a library material")); return {'CANCELLED'}
        directory = mat["kilnkit_src"]
        if not fn_scan_textures(directory):
            self.report({'WARNING'}, rpt_("No textures found in the source folder")); return {'CANCELLED'}
        fn_ensure_nodes(mat)
        fn_build_nodes(mat, directory, context.scene.kilnkit_scene_props.uv_method)
        mat.use_fake_user = True
        if mat.asset_data:
            try:
                mat.asset_generate_preview()
            except Exception:
                pass
        self.report({'INFO'}, rpt_("Rewired: {name}").format(name=mat.name))
        return {'FINISHED'}


class KILNKIT_OT_LibRemove(bpy.types.Operator):
    bl_idname = "kilnkit.lib_remove"; bl_label = "Delete"; bl_options = {'REGISTER', 'UNDO'}
    bl_description = "Delete this library material from the Blender data"
    mat_name: bpy.props.StringProperty()

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        mat = bpy.data.materials.get(self.mat_name)
        if not mat:
            self.report({'ERROR'}, rpt_("Material not found")); return {'CANCELLED'}
        name = mat.name
        bpy.data.materials.remove(mat)
        self.report({'INFO'}, rpt_("Deleted: {name}").format(name=name))
        return {'FINISHED'}


def fn_add_material_as_slot(obj, mat):
    """Add a material to the object as a new slot — fills the folder (kilnkit_src) and
       sliders from node values. Returns the slot index. The bridge that attaches a
       library material to a mesh (shared by LibAssign and SlotFromLibrary)."""
    obj.kilnkit_slots.add()
    idx = len(obj.kilnkit_slots) - 1
    obj.kilnkit_slots[idx].directory = mat.get("kilnkit_src", "")
    while len(obj.data.materials) <= idx:
        obj.data.materials.append(None)
    obj.data.materials[idx] = mat
    obj.active_material_index = idx
    uv, ao, normal, height = fn_read_slider_values(mat)
    obj.kilnkit_slots[idx].uv_scale        = uv
    obj.kilnkit_slots[idx].ao_strength     = ao
    obj.kilnkit_slots[idx].normal_strength = normal
    obj.kilnkit_slots[idx].height_scale    = height
    return idx


class KILNKIT_OT_LibAssign(bpy.types.Operator):
    bl_idname = "kilnkit.lib_assign"; bl_label = "Apply to Mesh"; bl_options = {'REGISTER', 'UNDO'}
    bl_description = "Add this library material to the active mesh as a slot (folder and sliders filled automatically — appears in the Main tab)"
    mat_name: bpy.props.StringProperty()

    def execute(self, context):
        obj = context.active_object
        if not obj or obj.type != 'MESH':
            self.report({'ERROR'}, rpt_("Select a mesh object")); return {'CANCELLED'}
        mat = bpy.data.materials.get(self.mat_name)
        if not mat:
            self.report({'ERROR'}, rpt_("Material not found")); return {'CANCELLED'}
        idx = fn_add_material_as_slot(obj, mat)
        context.scene.kilnkit_scene_props.active_slot_index = idx
        self.report({'INFO'}, rpt_("Applied: {mat} → {obj} (slot {n})").format(mat=mat.name, obj=obj.name, n=idx+1))
        return {'FINISHED'}


_lib_enum_cache = []   # keep references to dynamic enum strings (prevents GC corruption)

def _lib_material_items(self, context):
    global _lib_enum_cache
    _lib_enum_cache = [(m.name, m.name, "") for m in bpy.data.materials if m.get("kilnkit_lib")]
    if not _lib_enum_cache:
        _lib_enum_cache = [('', "(no library materials)", "")]
    return _lib_enum_cache


class KILNKIT_OT_SlotFromLibrary(bpy.types.Operator):
    bl_idname = "kilnkit.slot_from_library"; bl_label = "Import from Library"; bl_options = {'REGISTER', 'UNDO'}
    bl_description = "Pick an existing library material and add it to this mesh as a slot (no folder needed)"
    material: bpy.props.EnumProperty(name="Material", items=_lib_material_items)

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        self.layout.prop(self, "material")

    def execute(self, context):
        obj = context.active_object
        if not obj or obj.type != 'MESH':
            self.report({'ERROR'}, rpt_("Select a mesh object")); return {'CANCELLED'}
        mat = bpy.data.materials.get(self.material)
        if not mat:
            self.report({'WARNING'}, rpt_("No library materials")); return {'CANCELLED'}
        idx = fn_add_material_as_slot(obj, mat)
        context.scene.kilnkit_scene_props.active_slot_index = idx
        self.report({'INFO'}, rpt_("Imported: {name} (slot {n})").format(name=mat.name, n=idx+1))
        return {'FINISHED'}


class KILNKIT_OT_SlotsFromObject(bpy.types.Operator):
    bl_idname = "kilnkit.slots_from_object"; bl_label = "Slots from Materials"; bl_options = {'REGISTER', 'UNDO'}
    bl_description = "Recognize the materials already on this object (e.g. dragged from the Asset Browser) as add-on slots — folder and sliders restored when kilnkit_src exists"

    def execute(self, context):
        obj = context.active_object
        if not obj or obj.type != 'MESH':
            self.report({'ERROR'}, rpt_("Select a mesh object")); return {'CANCELLED'}
        present = [m for m in obj.data.materials if m]
        if not present:
            self.report({'WARNING'}, rpt_("This object has no materials")); return {'CANCELLED'}
        for i, mat in enumerate(obj.data.materials):
            if mat is None:
                continue
            while len(obj.kilnkit_slots) <= i:
                obj.kilnkit_slots.add()
            slot = obj.kilnkit_slots[i]
            if not slot.directory:
                slot.directory = mat.get("kilnkit_src", "")
            uv, ao, nrm, hgt = fn_read_slider_values(mat)
            slot.uv_scale        = uv
            slot.ao_strength     = ao
            slot.normal_strength = nrm
            slot.height_scale    = hgt
        context.scene.kilnkit_scene_props.active_slot_index = 0
        self.report({'INFO'}, rpt_("Recognized {n} materials as slots").format(n=len(present)))
        return {'FINISHED'}


class KILNKIT_OT_MakeSingleUser(bpy.types.Operator):
    bl_idname = "kilnkit.make_single_user"; bl_label = "Make Single-User"; bl_options = {'REGISTER', 'UNDO'}
    bl_description = "Detach this slot's material as a copy owned by this mesh only — its sliders become independent of other meshes"
    slot_index: bpy.props.IntProperty()

    def execute(self, context):
        obj = context.active_object
        if not obj or obj.type != 'MESH':
            self.report({'ERROR'}, rpt_("Select a mesh object")); return {'CANCELLED'}
        if (self.slot_index >= len(obj.data.materials)
                or not obj.data.materials[self.slot_index]):
            self.report({'ERROR'}, rpt_("No material")); return {'CANCELLED'}
        mat = obj.data.materials[self.slot_index]
        real_users = mat.users - (1 if mat.use_fake_user else 0)   # a fake user isn't real sharing
        if real_users <= 1:
            self.report({'INFO'}, rpt_("Already single-user")); return {'CANCELLED'}
        copy = mat.copy()
        # Mesh-only variant — clear library/asset tags (not a library entry).
        # kilnkit_src is kept (folder / rewire)
        copy.use_fake_user = False
        if "kilnkit_lib" in copy.keys():
            del copy["kilnkit_lib"]
        try:
            if copy.asset_data:
                copy.asset_clear()
        except Exception:
            pass
        obj.data.materials[self.slot_index] = copy
        self.report({'INFO'}, rpt_("Single-user copy: {name}").format(name=copy.name))
        return {'FINISHED'}


_libtarget_cache = []   # keep references to dynamic enum strings

def _asset_library_items(self, context):
    global _libtarget_cache
    _libtarget_cache = [(lab.name, lab.name, lab.path)
                        for lab in context.preferences.filepaths.asset_libraries if lab.path]
    if not _libtarget_cache:
        _libtarget_cache = [('', "(no asset libraries)", "")]
    return _libtarget_cache


def _preview_ready(mat):
    """Whether the material's asset preview is actually baked — cheaply sample a few
       pixels (never read them all: heavy and unstable). asset_generate_preview() is
       async, so right after a build the preview is empty and fills in the background."""
    pv = getattr(mat, "preview", None)
    if not pv:
        return False
    try:
        n = len(pv.image_pixels)
    except Exception:
        return False
    if n <= 0:
        return False
    return any(pv.image_pixels[i] for i in range(0, min(n, 4000), 37))


class KILNKIT_OT_LibExport(bpy.types.Operator):
    bl_idname = "kilnkit.lib_export"; bl_label = "Export to Library"; bl_options = {'REGISTER'}
    bl_description = "Export library materials to a Blender asset library folder (Kilnkit/), one .blend per material. One at a time (UI stays responsive), ESC to cancel"
    target: bpy.props.EnumProperty(name="Asset Library", items=_asset_library_items)

    _timer = None
    _libs = None
    _out_dir = ""
    _index = 0
    _count = 0
    _item_deadline = 0.0

    def invoke(self, context, event):
        paths = [lab for lab in context.preferences.filepaths.asset_libraries if lab.path]
        if not paths:
            self.report({'ERROR'}, rpt_("Add a path under Preferences > File Paths > Asset Libraries first"))
            return {'CANCELLED'}
        if len(paths) == 1:
            self.target = paths[0].name
            return self.execute(context)
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        self.layout.prop(self, "target")

    def execute(self, context):
        libs = [m for m in bpy.data.materials if m.get("kilnkit_lib")]
        if not libs:
            self.report({'WARNING'}, rpt_("No library materials to export")); return {'CANCELLED'}
        lib = next((lab for lab in context.preferences.filepaths.asset_libraries
                    if lab.name == self.target and lab.path), None)
        if not lib:
            self.report({'ERROR'}, rpt_("Asset library not found")); return {'CANCELLED'}
        out_dir = os.path.join(os.path.normpath(bpy.path.abspath(lib.path)), LIBRARY_SUBDIR)
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as e:
            self.report({'ERROR'}, rpt_("Failed to create folder: {err}").format(err=e)); return {'CANCELLED'}

        # Request async preview generation for unbaked materials (cheap) — prevents blank
        # thumbnails when exporting right after a build. Forced synchronous rendering
        # (wm.previews_ensure) is heavy and crash-prone, so it is not used.
        for m in libs:
            if not _preview_ready(m):
                try:
                    m.asset_generate_preview()
                except Exception:
                    pass

        self._libs = libs
        self._out_dir = out_dir
        self._index = 0
        self._count = 0

        # Headless (background) has no event loop for modals → process synchronously in one go
        if bpy.app.background or context.window is None:
            for mat in libs:
                if self._export_one(mat):
                    self._count += 1
            self.report({'INFO'}, rpt_("Exported {n} → {dir}").format(n=self._count, dir=out_dir))
            return {'FINISHED'}

        # With a display: one material per tick — UI stays responsive, progress shown
        self._item_deadline = time.time() + 8.0
        wm = context.window_manager
        self._timer = wm.event_timer_add(0.2, window=context.window)
        wm.modal_handler_add(self)
        self._status(context)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type == 'ESC':
            self._cleanup(context)
            self.report({'INFO'}, rpt_("Cancelled — exported {n} so far").format(n=self._count))
            return {'CANCELLED'}
        if event.type != 'TIMER':
            return {'PASS_THROUGH'}

        if self._index >= len(self._libs):
            return self._finish(context)

        mat = self._libs[self._index]
        # If this material's preview isn't baked yet and time remains, wait until the
        # next tick (Blender's background job fills it in)
        if mat and not _preview_ready(mat) and time.time() < self._item_deadline:
            return {'PASS_THROUGH'}

        # Process one item — next material on the next tick
        if self._export_one(mat):
            self._count += 1
        self._index += 1
        self._item_deadline = time.time() + 8.0   # reset the wait for the next material
        self._status(context)
        return {'PASS_THROUGH'}

    def _status(self, context):
        try:
            n = len(self._libs or [])
            if self._index < n:
                context.workspace.status_text_set(iface_("Library export {i}/{n}…  (ESC to cancel)").format(
                    i=self._index + 1, n=n))
            else:
                context.workspace.status_text_set(None)
        except Exception:
            pass

    def _finish(self, context):
        self._cleanup(context)
        self.report({'INFO'}, rpt_("Exported {n} → {dir} (Asset Browser may need a refresh)").format(
            n=self._count, dir=self._out_dir))
        return {'FINISHED'}

    def _cleanup(self, context):
        if self._timer is not None:
            try:
                context.window_manager.event_timer_remove(self._timer)
            except Exception:
                pass
            self._timer = None
        try:
            context.workspace.status_text_set(None)
        except Exception:
            pass

    def cancel(self, context):
        self._cleanup(context)

    def _export_one(self, mat):
        """One material only — pack just its textures → write the .blend → unpack only what
           we packed. Returns success. Per-material pack/restore keeps memory growth to one
           material's worth, and one-per-tick keeps the UI responsive."""
        if not mat:
            return False
        packed_now = []
        if mat.node_tree:
            for node in mat.node_tree.nodes:
                if node.type == 'TEX_IMAGE' and node.image and not node.image.packed_file:
                    try:
                        node.image.pack(); packed_now.append(node.image)
                    except Exception:
                        pass
        ok = False
        filepath = os.path.join(self._out_dir, f"{mat.name}.blend")
        try:
            bpy.data.libraries.write(filepath, {mat}, fake_user=True, compress=True)
            ok = True
        except Exception as e:
            self.report({'WARNING'}, rpt_("{name} export failed: {err}").format(name=mat.name, err=e))
        # Restore the current file — unpack only the images we packed
        for img in packed_now:
            try:
                img.unpack(method='USE_ORIGINAL')
            except Exception:
                pass
        return ok


class KILNKIT_OT_OpenAssetBrowser(bpy.types.Operator):
    bl_idname = "kilnkit.open_asset_browser"; bl_label = "Open Asset Browser"; bl_options = {'REGISTER'}
    bl_description = "Open the Blender Asset Browser in a new window — browse exported library materials and drag them onto meshes"

    def execute(self, context):
        try:
            bpy.ops.wm.window_new()
        except Exception as e:
            self.report({'ERROR'}, rpt_("Cannot open a new window: {err}").format(err=e)); return {'CANCELLED'}
        win = context.window_manager.windows[-1]
        try:
            win.screen.areas[0].ui_type = 'ASSETS'
        except Exception as e:
            self.report({'ERROR'}, rpt_("Failed to switch to the Asset Browser: {err}").format(err=e)); return {'CANCELLED'}
        self.report({'INFO'}, rpt_("Asset Browser opened in a new window"))
        return {'FINISHED'}


# ── Mesh optimization ──────────────────────────────────────────

class KILNKIT_OT_Decimate(bpy.types.Operator):
    bl_idname = "kilnkit.decimate"; bl_label = "Apply Decimate"; bl_options = {'REGISTER', 'UNDO'}
    bl_description = "Apply a Decimate modifier to reduce the polygon count"

    def execute(self, context):
        obj = context.active_object
        if not obj or obj.type != 'MESH':
            self.report({'ERROR'}, rpt_("Select a mesh object")); return {'CANCELLED'}
        fn_ensure_object_mode()
        sp = context.scene.kilnkit_scene_props
        for mod in obj.modifiers:
            if mod.type == 'DECIMATE':
                obj.modifiers.remove(mod)
        mod = obj.modifiers.new(name="KK_Decimate", type='DECIMATE')
        mod.ratio = sp.decimate_ratio
        window, area, region = fn_get_view3d()
        try:
            if area:
                with bpy.context.temp_override(window=window, area=area, region=region):
                    bpy.ops.object.modifier_apply(modifier="KK_Decimate")
            else:
                bpy.ops.object.modifier_apply(modifier="KK_Decimate")
        except Exception as e:
            self.report({'ERROR'}, rpt_("Decimate failed: {err}").format(err=e)); return {'CANCELLED'}
        self.report({'INFO'}, rpt_("Decimate done — polygons: {n:,}").format(n=len(obj.data.polygons)))
        return {'FINISHED'}


def fn_lod_base_name(name):
    """Strip a trailing _LOD<n> and return the base name (prevents _LOD0_LOD0 on re-runs)."""
    return re.sub(r'_LOD\d+$', '', name)


class KILNKIT_OT_GenerateLOD(bpy.types.Operator):
    bl_idname = "kilnkit.generate_lod"; bl_label = "Generate LODs"; bl_options = {'REGISTER', 'UNDO'}
    bl_description = "Create a LOD set (_LOD0.._LODn) for each selected mesh. Originals preserved; each LOD gets a non-destructive Decimate modifier"

    def execute(self, context):
        sp = context.scene.kilnkit_scene_props
        targets = [o for o in context.selected_objects if o.type == 'MESH']
        if not targets:
            self.report({'ERROR'}, rpt_("Select a mesh object")); return {'CANCELLED'}
        count, step = sp.lod_count, sp.lod_step
        made = 0
        for obj in targets:
            base = fn_lod_base_name(obj.name)
            coll = obj.users_collection[0] if obj.users_collection else context.scene.collection
            # Remove the existing LOD set (idempotent re-runs) — the original obj is untouched
            pat = re.compile(re.escape(base) + r'_LOD\d+$')
            for ex in list(bpy.data.objects):
                if ex != obj and ex.type == 'MESH' and pat.match(ex.name):
                    bpy.data.objects.remove(ex, do_unlink=True)
            for i in range(count):
                dup = obj.copy()
                dup.data = obj.data.copy()
                dup.name = f"{base}_LOD{i}"
                dup.data.name = dup.name
                for mod in [mm for mm in dup.modifiers if mm.type == 'DECIMATE']:
                    dup.modifiers.remove(mod)
                ratio = step ** i
                if ratio < 0.999:
                    m = dup.modifiers.new(name="KK_LOD", type='DECIMATE')
                    m.ratio = ratio
                coll.objects.link(dup)
                made += 1
        self.report({'INFO'}, rpt_("Created {n} LODs ({m} meshes × {c} levels)").format(n=made, m=len(targets), c=count))
        return {'FINISHED'}


# ── Batch execution ────────────────────────────────────────────

class KILNKIT_OT_BatchRun(bpy.types.Operator):
    bl_idname = "kilnkit.batch_run"; bl_label = "Run Batch"; bl_options = {'REGISTER', 'UNDO'}
    bl_description = "Apply the active object's slot setup to all selected meshes"

    def execute(self, context):
        active = context.active_object
        sp     = context.scene.kilnkit_scene_props
        if not active:
            self.report({'ERROR'}, rpt_("Select an active object")); return {'CANCELLED'}
        targets = [o for o in context.selected_objects if o.type == 'MESH']
        if not targets:
            self.report({'ERROR'}, rpt_("Select a mesh object")); return {'CANCELLED'}
        active_slots = active.kilnkit_slots
        if not active_slots:
            self.report({'ERROR'}, rpt_("The active object has no slots")); return {'CANCELLED'}

        slider_vals = []
        for i in range(len(active_slots)):
            mat = active.data.materials[i] if i < len(active.data.materials) else None
            slider_vals.append(fn_read_slider_values(mat))

        success = 0
        fail_names: list = []
        for obj in targets:
            # KEEP samples the mesh's own UV map — without one the textures collapse
            # to a single point, so skip the object instead of producing garbage.
            if sp.uv_method == 'KEEP' and not fn_mesh_has_uv(obj):
                fail_names.append(rpt_("{name} (no UV map)").format(name=obj.name))
                continue

            # Sync slot directories
            for i in range(len(active_slots)):
                while len(obj.kilnkit_slots) <= i:
                    obj.kilnkit_slots.add()
                obj.kilnkit_slots[i].directory = active_slots[i].directory

            obj_ok = True
            for i in range(len(active_slots)):
                if not active_slots[i].directory:
                    fail_names.append(rpt_("{name} (slot {n})").format(name=obj.name, n=i+1))
                    obj_ok = False
                    continue
                dir_path = os.path.normpath(bpy.path.abspath(active_slots[i].directory))
                mat_name = f"KK_{os.path.basename(dir_path)}"

                src = active_slots[i].directory
                # Same material-decision rule as the main path (fn_run_pipeline) —
                # one source of truth. Identity = source folder path: a fresh target
                # shares the active's material via fn_find_object_material_by_src(src).
                mat = fn_resolve_material(obj, mat_name, i, sp.mat_conflict, src)

                bpy.context.view_layer.objects.active = obj
                if sp.step_scale:
                    if not fn_apply_scale(obj):
                        self.report({'WARNING'}, rpt_("{name}: Shape Keys present — skipped scale apply").format(name=obj.name))
                if sp.step_uv:
                    if sp.uv_method == 'UV':
                        fn_smart_uv(obj, sp)
                    elif sp.uv_method == 'CUBE':
                        fn_cube_uv(obj, sp)
                    elif sp.uv_method == 'SLIM':
                        fn_slim_uv(obj, sp)
                    # TRIPLANAR/OBJECT — coordinate-based; KEEP — reuses the existing UVs. Skip either way.
                if sp.step_pbr:
                    if sp.mat_conflict == 'OVERWRITE' and obj != active:
                        fn_apply_slider_values(mat, *slider_vals[i])
                    else:
                        fn_build_nodes(mat, active_slots[i].directory, sp.uv_method)
                        fn_apply_slider_values(mat, *slider_vals[i])
            if obj_ok:
                success += 1

        bpy.context.view_layer.objects.active = active
        if fail_names:
            preview = ", ".join(fail_names[:3])
            extra = rpt_(" and {n} more").format(n=len(fail_names)-3) if len(fail_names) > 3 else ""
            self.report({'WARNING'}, rpt_("Batch done — succeeded: {n} / failed: {list}{extra}").format(
                n=success, list=preview, extra=extra))
        else:
            self.report({'INFO'}, rpt_("Batch done — succeeded: {n}").format(n=success))
        return {'FINISHED'}


# ── Naming ─────────────────────────────────────────────────────

def fn_naming_prefix(sp):
    """Normalized prefix — inserts _ when no separator (_ - . space) is present. Empty string when unused."""
    prefix = sp.naming_prefix.strip() if sp.naming_use_prefix else ""
    if prefix and not prefix.endswith(('_', '-', '.', ' ')):
        prefix += '_'
    return prefix


def fn_naming_final(sp, base):
    """Final object/mesh name = prefix + base."""
    return f"{fn_naming_prefix(sp)}{base}"


def fn_naming_material_name(sp, base, slot_folder, multi):
    """Material name — Blender Studio style (the asset name is stamped into every datablock).
    Single slot: <prefix><base>. Multi: <prefix><base>_<slot folder> (shortened when folder == base)."""
    if multi and slot_folder and slot_folder != base:
        core = f"{base}_{slot_folder}"
    else:
        core = base
    return f"{fn_naming_prefix(sp)}{core}"


def fn_naming_base(sp, obj):
    """Naming base — user input (naming_base) first, else the first slot's folder name. None when neither."""
    b = sp.naming_base.strip()
    if b:
        return b
    return fn_first_slot_dir(obj)[1]


def fn_first_slot_dir(obj):
    """Reference slot for naming — returns (index, folder basename) of the first slot with a
    folder. Default is slot[0]; when slot[0] has no folder, falls back to the first slot
    that has one. (None, None) when nothing qualifies."""
    for i, s in enumerate(obj.kilnkit_slots):
        if s.directory:
            dir_path = os.path.normpath(bpy.path.abspath(s.directory))
            return i, os.path.basename(dir_path)
    return None, None


def fn_slot_folder_name(obj, slot_index):
    """Slot folder basename (None if unset) — used for material renaming."""
    slots = obj.kilnkit_slots
    if slot_index < len(slots) and slots[slot_index].directory:
        return os.path.basename(os.path.normpath(bpy.path.abspath(slots[slot_index].directory)))
    return None


class KILNKIT_OT_ApplyNaming(bpy.types.Operator):
    bl_idname = "kilnkit.apply_naming"; bl_label = "Apply Naming"; bl_options = {'REGISTER', 'UNDO'}
    bl_description = "Rename the object, mesh, and materials as one family based on the asset name (folder name when empty)"

    def invoke(self, context, event):
        sp      = context.scene.kilnkit_scene_props
        targets = [o for o in context.selected_objects if o.type == 'MESH']

        # Collision detection — clashes with existing objects, or several batch items
        # converging on the same name
        existing_names = {o.name for o in bpy.data.objects}
        self._duplicates = []
        seen = set()
        for obj in targets:
            base = fn_naming_base(sp, obj)
            if not base:
                continue
            final = fn_naming_final(sp, base)
            if (final in existing_names and obj.name != final) or final in seen:
                self._duplicates.append(final)
            seen.add(final)

        if self._duplicates:
            return context.window_manager.invoke_props_dialog(self, width=320)
        return self.execute(context)

    def draw(self, context):
        l = self.layout
        l.label(text="Duplicate names detected", icon='ERROR')
        l.separator()
        for name in list(set(self._duplicates))[:5]:
            l.label(text=iface_("  · {name} — already exists").format(name=name), icon='DOT')
        if len(self._duplicates) > 5:
            l.label(text=iface_("  … and {n} more").format(n=len(self._duplicates)-5))
        l.separator()
        l.label(text="Continuing lets Blender append .001 automatically.")

    def execute(self, context):
        sp      = context.scene.kilnkit_scene_props
        targets = [o for o in context.selected_objects if o.type == 'MESH']
        if not targets:
            self.report({'ERROR'}, rpt_("Select a mesh object")); return {'CANCELLED'}

        changed, skipped = 0, 0

        for obj in targets:
            base = fn_naming_base(sp, obj)
            if not base:
                skipped += 1; continue

            final    = fn_naming_final(sp, base)
            obj.name = final
            if obj.data:
                obj.data.name = final
            # Rename materials into the same family (toggle) — single: <name>, multi: <name>_<slot folder>
            if sp.naming_rename_mats:
                mats  = [m for m in obj.data.materials if m]
                multi = len(mats) > 1
                for i, m in enumerate(obj.data.materials):
                    if not m:
                        continue
                    m.name = fn_naming_material_name(sp, base, fn_slot_folder_name(obj, i), multi)
            changed += 1

        msg = rpt_("Renamed {n} objects").format(n=changed)
        if skipped:
            msg += rpt_(" ({n} skipped — no folder/name)").format(n=skipped)
        self.report({'INFO'}, msg)
        return {'FINISHED'}


class KILNKIT_OT_RemoveNumberSuffix(bpy.types.Operator):
    bl_idname = "kilnkit.remove_number_suffix"; bl_label = "Remove .001 Suffix"; bl_options = {'REGISTER', 'UNDO'}
    bl_description = "Strip trailing .001-style numbers from object, data, and material names"

    def execute(self, context):
        targets = [o for o in context.selected_objects if o.type == 'MESH']
        if not targets:
            self.report({'ERROR'}, rpt_("Select a mesh object")); return {'CANCELLED'}

        pattern = re.compile(r'\.\d{3}$')
        changed = 0
        for obj in targets:
            obj.name = pattern.sub('', obj.name)
            if obj.data:
                obj.data.name = pattern.sub('', obj.data.name)
            for mat in obj.data.materials:
                if mat:
                    mat.name = pattern.sub('', mat.name)
            changed += 1

        self.report({'INFO'}, rpt_("Removed number suffixes on {n} objects").format(n=changed))
        return {'FINISHED'}


# ── File drop handler ──────────────────────────────────────────

class KILNKIT_FH_TextureDrop(bpy.types.FileHandler):
    """Drop a texture file into the viewport → its parent folder auto-links to the active slot."""
    bl_idname      = "KILNKIT_FH_texture_drop"
    bl_label       = "Kilnkit: Link Texture Folder"
    bl_import_operator = "kilnkit.drop_texture"
    bl_file_extensions = ";".join(SUPPORTED_EXTS)

    @classmethod
    def poll_drop(cls, context):
        return (context.area and context.area.type == 'VIEW_3D'
                and context.active_object and context.active_object.type == 'MESH')


class KILNKIT_OT_DropTexture(bpy.types.Operator):
    bl_idname     = "kilnkit.drop_texture"
    bl_label      = "Drop Texture"
    bl_options    = {'REGISTER', 'UNDO'}
    bl_description = "Link the dropped texture file's parent folder to the active slot"

    filepath:  bpy.props.StringProperty(subtype='FILE_PATH')
    directory: bpy.props.StringProperty(subtype='DIR_PATH')

    def execute(self, context):
        obj = context.active_object
        if not obj or obj.type != 'MESH':
            self.report({'ERROR'}, rpt_("Select a mesh object")); return {'CANCELLED'}

        # Detect the dropped file's parent folder
        drop_path = self.filepath or self.directory
        if not drop_path:
            return {'CANCELLED'}
        folder = os.path.dirname(bpy.path.abspath(drop_path))

        sp  = context.scene.kilnkit_scene_props
        idx = sp.active_slot_index

        # Auto-add a slot when none exist
        if not obj.kilnkit_slots:
            obj.kilnkit_slots.add()
            idx = 0
        elif idx >= len(obj.kilnkit_slots):
            idx = len(obj.kilnkit_slots) - 1

        obj.kilnkit_slots[idx].directory = folder
        sp.active_slot_index = idx
        self.report({'INFO'}, rpt_("Folder linked: {name}").format(name=os.path.basename(folder)))
        return {'FINISHED'}


# ── Utilities ──────────────────────────────────────────────────

class KILNKIT_OT_CleanupImages(bpy.types.Operator):
    bl_idname = "kilnkit.cleanup_images"; bl_label = "Clean Up Images"; bl_options = {'REGISTER', 'UNDO'}
    bl_description = "Remove images with no users from the Blender data"

    def execute(self, context):
        targets = [img for img in bpy.data.images if img.users == 0]
        count = len(targets)
        for img in targets:
            bpy.data.images.remove(img)
        self.report({'INFO'}, rpt_("Removed {n} images").format(n=count))
        return {'FINISHED'}


def _dup_signature(mat):
    """Duplicate signature — texture set + mapping + slider/node values.

    The mapping (coordinate source + projection) belongs in the signature: two materials
    with identical textures but different mappings are *not* duplicates, and merging them
    would silently re-map one object. Without it, an Object-coords material and a
    UV-coords material hash the same (both FLAT).
    """
    if not mat.node_tree:
        return None
    nodes = mat.node_tree.nodes
    imgs = sorted({os.path.normcase(os.path.normpath(bpy.path.abspath(n.image.filepath)))
                   for n in nodes if n.type == 'TEX_IMAGE' and n.image and n.image.filepath})
    if not imgs:
        return None
    def r(v):
        return round(float(v), 3)
    vals = [("coord", fn_material_coord_source(mat) or "?"),
            ("proj", tuple(sorted({n.projection for n in nodes if n.type == 'TEX_IMAGE'})))]
    if "KK_Mapping" in nodes:
        s = nodes["KK_Mapping"].inputs[3].default_value
        vals.append(("uv", r(s[0]), r(s[1]), r(s[2])))
    if "KK_AO_Mix" in nodes:
        vals.append(("ao", r(nodes["KK_AO_Mix"].inputs[0].default_value)))
    if "KK_NormalMap" in nodes:
        vals.append(("nrm", r(nodes["KK_NormalMap"].inputs[0].default_value)))
    if "KK_Displacement" in nodes:
        vals.append(("h", r(nodes["KK_Displacement"].inputs[2].default_value)))
    for n in nodes:
        if n.type == 'TEX_IMAGE' and n.projection == 'BOX':
            vals.append(("blend", r(n.projection_blend)))
            break
    return (tuple(imgs), tuple(vals))


def _pick_canonical(mats):
    """Which material a duplicate group keeps — library > no number suffix > shorter name > alphabetical."""
    def score(m):
        suffixed = 1 if re.search(r'\.\d{3}$', m.name) else 0
        return (0 if m.get("kilnkit_lib") else 1, suffixed, len(m.name), m.name)
    return sorted(mats, key=score)[0]


def fn_find_duplicate_materials():
    """Return material groups with identical texture sets — [(keeper, [dupes...]), ...] (groups of 2+ only)."""
    groups = {}
    for mat in bpy.data.materials:
        sig = _dup_signature(mat)
        if sig:
            groups.setdefault(sig, []).append(mat)
    out = []
    for mats in groups.values():
        if len(mats) >= 2:
            canon = _pick_canonical(mats)
            out.append((canon, [m for m in mats if m != canon]))
    return out


class KILNKIT_OT_DedupMaterials(bpy.types.Operator):
    bl_idname = "kilnkit.dedup_materials"; bl_label = "Merge Duplicate Materials"; bl_options = {'REGISTER', 'UNDO'}
    bl_description = "Merge duplicate materials that share the same textures into one and remap their users (.001 cleanup)"

    def invoke(self, context, event):
        self._groups = fn_find_duplicate_materials()
        if not self._groups:
            self.report({'INFO'}, rpt_("No duplicate materials")); return {'CANCELLED'}
        return context.window_manager.invoke_props_dialog(self, width=360)

    def draw(self, context):
        l = self.layout
        total = sum(len(d) for _, d in self._groups)
        l.label(text=iface_("{g} duplicate groups · {m} materials to merge").format(
            g=len(self._groups), m=total), icon='MATERIAL')
        l.separator()
        for canon, dups in self._groups[:6]:
            l.label(text=f"{canon.name}  ←  {', '.join(d.name for d in dups)}", icon='DOT')
        if len(self._groups) > 6:
            l.label(text=iface_("  … and {n} more groups").format(n=len(self._groups)-6))
        l.separator()
        l.label(text="Duplicates are deleted and their users remapped to the kept material.")

    def execute(self, context):
        groups = getattr(self, "_groups", None)
        if groups is None:
            groups = fn_find_duplicate_materials()
        merged = 0
        for canon, dups in groups:
            for d in dups:
                try:
                    d.user_remap(canon)
                    bpy.data.materials.remove(d)
                    merged += 1
                except Exception:
                    pass
        self.report({'INFO'}, rpt_("Merged {n} duplicate materials").format(n=merged))
        return {'FINISHED'}


class KILNKIT_OT_ResetSuffix(bpy.types.Operator):
    bl_idname = "kilnkit.reset_suffix"; bl_label = "Reset Suffix Defaults"; bl_options = {'REGISTER'}
    bl_description = "Restore the custom suffix settings to their defaults"

    def execute(self, context):
        prefs = context.preferences.addons.get(__package__)
        if prefs:
            p = prefs.preferences
            p.suffix_preset = 'CUSTOM'
            for key, val in PBR_RULES_DEFAULT.items():
                setattr(p, f"suffix_{key}", val)
        self.report({'INFO'}, rpt_("Suffixes reset to defaults"))
        return {'FINISHED'}


class KILNKIT_OT_ResetSettings(bpy.types.Operator):
    bl_idname = "kilnkit.reset_settings"; bl_label = "Reset to Defaults"; bl_options = {'REGISTER', 'UNDO'}
    bl_description = "Reset pipeline settings (UV, steps, Decimate, etc.) to their defaults"

    def execute(self, context):
        sp = context.scene.kilnkit_scene_props
        sp.step_scale       = True
        sp.step_uv          = True
        sp.step_pbr         = True
        sp.uv_angle_limit   = DEFAULT_UV_ANGLE
        sp.use_pack_islands = True
        sp.pack_rotate      = True
        sp.pack_margin      = DEFAULT_PACK_MARGIN
        sp.decimate_ratio   = DEFAULT_DECIMATE
        sp.mat_conflict     = 'OVERWRITE'
        sp.uv_method        = 'TRIPLANAR'
        sp.auto_sync        = False
        self.report({'INFO'}, rpt_("Settings reset to defaults"))
        return {'FINISHED'}


# ── Node value sync ────────────────────────────────────────────

class KILNKIT_OT_SyncFromNodes(bpy.types.Operator):
    bl_idname = "kilnkit.sync_from_nodes"; bl_label = "Pull Node Values"; bl_options = {'REGISTER', 'UNDO'}
    bl_description = "Mirror node values edited in the Shader Editor into the active slot's sliders"

    def execute(self, context):
        sync_active_slot_from_nodes(context)
        self.report({'INFO'}, rpt_("Node values pulled into sliders"))
        return {'FINISHED'}


# ================================================================
# Render output automation — lighting / environment
#   Turns the chores (lighting setup, background) into buttons. Built-in bpy only →
#   keeps zero core dependencies. Idempotent re-runs: found and replaced by the
#   KK_World / KK_Render_Lights names.
# ================================================================

def fn_ensure_kk_world():
    """Ensure a node-based KK_World — wires Background (KK_WORLD_BG) → World Output. Returns (world, bg_node)."""
    w = bpy.data.worlds.get(KK_WORLD_NAME) or bpy.data.worlds.new(KK_WORLD_NAME)
    fn_ensure_nodes(w)
    nt = w.node_tree
    bg = nt.nodes.get(KK_WORLD_BG)
    if not bg:
        bg = next((n for n in nt.nodes if n.type == 'BACKGROUND'), None)
        if not bg:
            bg = nt.nodes.new("ShaderNodeBackground")
        bg.name = KK_WORLD_BG
        bg.location = (-200, 0)
    out = next((n for n in nt.nodes if n.type == 'OUTPUT_WORLD'), None)
    if not out:
        out = nt.nodes.new("ShaderNodeOutputWorld")
        out.location = (100, 0)
    if not bg.outputs[0].links:
        nt.links.new(bg.outputs[0], out.inputs[0])
    return w, bg


def fn_clear_kk_lights():
    """Empty the light objects (+ orphaned light data) in the KK_Render_Lights collection. Returns the collection (None if absent)."""
    coll = bpy.data.collections.get(KK_LIGHTS_COLL)
    if coll:
        for ob in list(coll.objects):
            data = ob.data if ob.type == 'LIGHT' else None
            bpy.data.objects.remove(ob, do_unlink=True)
            if data and data.users == 0:
                try:
                    bpy.data.lights.remove(data)
                except Exception:
                    pass
    return coll


class KILNKIT_OT_SetupEnvironment(bpy.types.Operator):
    bl_idname = "kilnkit.setup_environment"; bl_label = "Apply Environment"; bl_options = {'REGISTER', 'UNDO'}
    bl_description = "Set up the world from the chosen environment preset (Studio/HDRI/Flat) — built-in bpy only (zero dependencies)"

    def execute(self, context):
        sp = context.scene.kilnkit_scene_props
        w, bg = fn_ensure_kk_world()
        nt = w.node_tree
        env = nt.nodes.get(KK_WORLD_ENV)
        preset = sp.render_light_preset

        if preset == 'HDRI':
            path = bpy.path.abspath(sp.render_hdri_path) if sp.render_hdri_path else ""
            if not path or not os.path.isfile(path):
                self.report({'ERROR'}, rpt_("Set an HDRI file first (.hdr/.exr)"))
                return {'CANCELLED'}
            try:
                img = bpy.data.images.load(path, check_existing=True)
            except Exception as e:
                self.report({'ERROR'}, rpt_("Failed to load HDRI: {err}").format(err=e))
                return {'CANCELLED'}
            if not env:
                env = nt.nodes.new("ShaderNodeTexEnvironment")
                env.name = KK_WORLD_ENV
                env.location = (-500, 0)
            env.image = img
            nt.links.new(env.outputs[0], bg.inputs["Color"])
            bg.inputs["Strength"].default_value = sp.world_strength
            msg = rpt_("HDRI environment applied: {name}").format(name=os.path.basename(path))
        else:
            # FLAT / STUDIO shared — remove the environment texture, then flat background
            # (STUDIO's lights are separate)
            if env:
                for l in list(bg.inputs["Color"].links):
                    nt.links.remove(l)
                nt.nodes.remove(env)
            c = sp.world_color
            bg.inputs["Color"].default_value = (c[0], c[1], c[2], 1.0)
            bg.inputs["Strength"].default_value = sp.world_strength
            msg = (rpt_("Flat color environment applied") if preset == 'FLAT'
                   else rpt_("Studio environment (neutral backdrop) applied — press 'Set Up 3-Point Lights' for the lights"))

        context.scene.world = w
        self.report({'INFO'}, msg)
        return {'FINISHED'}


# (angle: azimuth az / elevation el, deg) — az 0 = front (looking from -Y toward +Y), el upward
_CAM_ANGLES = {
    'THREEQ': (-35.0, 22.0),
    'FRONT':  (0.0, 8.0),
    'SIDE':   (-90.0, 8.0),
    'TOP':    (0.0, 80.0),
}


def fn_selected_bbox(objs):
    """Combined world-space bounding box of the object list → (center, size) Vectors. None when empty."""
    mn = [float('inf')] * 3
    mx = [float('-inf')] * 3
    found = False
    for o in objs:
        if o.type not in {'MESH', 'CURVE', 'SURFACE', 'FONT', 'META'}:
            continue
        for corner in o.bound_box:
            wc = o.matrix_world @ mathutils.Vector(corner)
            for k in range(3):
                if wc[k] < mn[k]:
                    mn[k] = wc[k]
                if wc[k] > mx[k]:
                    mx[k] = wc[k]
            found = True
    if not found:
        return None
    center = mathutils.Vector(((mn[0] + mx[0]) / 2, (mn[1] + mx[1]) / 2, (mn[2] + mx[2]) / 2))
    size = mathutils.Vector((mx[0] - mn[0], mx[1] - mn[1], mx[2] - mn[2]))
    return center, size


_CAM_VIEW_LABELS = {'THREEQ': "3/4 View", 'FRONT': "Front View", 'SIDE': "Side View", 'TOP': "Top View"}


def fn_orbit_direction(az_deg, el_deg):
    """Unit vector from a target's center toward a viewer at (azimuth, elevation).
       az 0 = front (the -Y side), positive az swings toward +X; el = degrees above the horizon."""
    az, el = math.radians(az_deg), math.radians(el_deg)
    return mathutils.Vector((
        math.sin(az) * math.cos(el),
        -math.cos(az) * math.cos(el),
        math.sin(el),
    ))


def fn_camera_frame_tans(cam_data, scene):
    """tan of the half field-of-view on the camera's horizontal and vertical axes.

    Both axes matter. sensor_width describes only the *wide* axis, so fitting to it alone
    crops the narrow one on any non-square resolution (1920x1080 loses top and bottom)."""
    r = scene.render
    ax = max(r.resolution_x * r.pixel_aspect_x, 1e-6)
    ay = max(r.resolution_y * r.pixel_aspect_y, 1e-6)
    fit = cam_data.sensor_fit
    if fit == 'VERTICAL':
        sy = cam_data.sensor_height
        sx = sy * ax / ay
    elif fit == 'HORIZONTAL':
        sx = cam_data.sensor_width
        sy = sx * ay / ax
    else:   # AUTO — sensor_width spans whichever render axis is longer
        sx = cam_data.sensor_width if ax >= ay else cam_data.sensor_width * ax / ay
        sy = cam_data.sensor_width if ay > ax else cam_data.sensor_width * ay / ax
    lens = max(cam_data.lens, 1e-6)
    return (sx / 2.0) / lens, (sy / 2.0) / lens


def fn_frame_points(context, targets):
    """World-space points the camera has to contain — evaluated mesh vertices, else bbox corners.

    The silhouette is what the viewer sees, so fit to it. A bounding *sphere* frames a flat asset
    far too loosely (Suzanne's diagonal is ~1.9x her height, so she lands tiny in frame), while
    bounding-box *corners* frame a round asset too loosely (a sphere's box corners stick out by
    sqrt(3)). Vertices sidestep both. numpy keeps a high-poly mesh cheap."""
    import numpy as np
    dg = context.evaluated_depsgraph_get()
    chunks = []
    for o in targets:
        ev = o.evaluated_get(dg)
        me = ev.data if ev.type == 'MESH' else None
        if me is not None and len(me.vertices):
            co = np.empty(len(me.vertices) * 3, dtype=np.float64)
            me.vertices.foreach_get('co', co)
            co = co.reshape(-1, 3)
        else:
            co = np.array([list(c) for c in o.bound_box], dtype=np.float64)
        m = np.array(ev.matrix_world, dtype=np.float64)
        chunks.append(co @ m[:3, :3].T + m[:3, 3])
    return np.concatenate(chunks) if chunks else None


def fn_fit_camera(points, center, direction, cam_data, scene, margin):
    """Aim point and distance that centre the silhouette and fill 1/margin of the tighter axis.

    The aim drifts off the bounding-box centre on purpose: a projected silhouette is not
    symmetric about that centre — Suzanne's chin reaches further down than her crown reaches
    up — so aiming there pins the asset against one edge and wastes the far side of the frame.
    Aim and distance depend on each other (moving the camera changes what projects widest),
    so they settle together over a few passes."""
    import numpy as np
    q = (-direction).to_track_quat('-Z', 'Y')
    right = np.array(q @ mathutils.Vector((1.0, 0.0, 0.0)))
    up = np.array(q @ mathutils.Vector((0.0, 1.0, 0.0)))
    fwd = np.array(direction)
    tan_x, tan_y = fn_camera_frame_tans(cam_data, scene)
    tx, ty = max(tan_x / margin, 1e-6), max(tan_y / margin, 1e-6)
    aim = np.array(center, dtype=np.float64)

    def _fit(a):
        rel = points - a
        toward_cam = rel @ fwd   # a point nearer the camera projects larger
        x, y = rel @ right, rel @ up
        d = float(max((toward_cam + np.abs(x) / tx).max(), (toward_cam + np.abs(y) / ty).max()))
        return d, toward_cam, x, y

    for _ in range(4):
        dist, toward_cam, x, y = _fit(aim)
        depth = np.maximum(dist - toward_cam, 1e-6)
        sx, sy = x / depth, y / depth
        off_x = float(sx.max() + sx.min()) / 2.0
        off_y = float(sy.max() + sy.min()) / 2.0
        if abs(off_x) < 1e-4 and abs(off_y) < 1e-4:
            break
        aim = aim + right * (off_x * dist) + up * (off_y * dist)
    else:
        dist = _fit(aim)[0]   # last pass moved the aim — refit so the frame still holds
    return mathutils.Vector(aim), dist


def fn_place_camera(context, targets, view, lens, margin):
    """Place and aim KK_Camera so the targets' silhouette fills the frame at the given view
       angle, and set it as the scene camera. Returns cam (None on failure). Shared by
       SetupCamera, multi-angle and turntable — deterministic, no nested bpy.ops."""
    bb = fn_selected_bbox(targets)
    pts = fn_frame_points(context, targets)
    if not bb or pts is None or not len(pts):
        return None
    center, size = bb
    radius = max(size.length / 2.0, 1e-4)   # diagonal radius — only for clipping and the near guard
    cam = bpy.data.objects.get("KK_Camera")
    if not cam or cam.type != 'CAMERA':
        cam = bpy.data.objects.new("KK_Camera", bpy.data.cameras.new("KK_Camera"))
        context.scene.collection.objects.link(cam)
    # A prior turntable parents KK_Camera to the orbit pivot. Placement below sets a
    # world-space location/aim, so detach first — otherwise (location is parent-local)
    # the camera lands in the wrong spot whenever the pivot is rotated (frame != 1).
    if cam.parent is not None:
        cam.parent = None
        cam.matrix_parent_inverse.identity()
    cam.data.lens = lens
    cam.data.sensor_fit = 'AUTO'
    az, el = _CAM_ANGLES.get(view, _CAM_ANGLES['THREEQ'])
    direction = fn_orbit_direction(az, el)
    aim, dist = fn_fit_camera(pts, center, direction, cam.data, context.scene, margin)
    dist = max(dist, radius * 1.05)   # never inside the asset
    cam.location = aim + direction * dist
    look = aim - cam.location
    cam.rotation_euler = look.to_track_quat('-Z', 'Y').to_euler()
    cam.data.clip_end = max(cam.data.clip_end, dist + radius * 4.0)
    context.scene.camera = cam
    return cam


def fn_camera_targets(context):
    """Camera target meshes — selected meshes first, else the active mesh."""
    targets = [o for o in context.selected_objects if o.type == 'MESH']
    if not targets:
        act = context.active_object
        if act and act.type == 'MESH':
            targets = [act]
    return targets


class KILNKIT_OT_SetupCamera(bpy.types.Operator):
    bl_idname = "kilnkit.setup_camera"; bl_label = "Auto-Place Camera"; bl_options = {'REGISTER', 'UNDO'}
    bl_description = "Place and aim the camera so the selected asset fills the frame (bounding-box based; re-running replaces it)"

    def execute(self, context):
        sp = context.scene.kilnkit_scene_props
        targets = fn_camera_targets(context)
        if not targets:
            self.report({'ERROR'}, rpt_("Select a mesh object"))
            return {'CANCELLED'}
        cam = fn_place_camera(context, targets, sp.camera_view, sp.camera_lens, sp.camera_margin)
        if not cam:
            self.report({'ERROR'}, rpt_("Cannot compute a bounding box"))
            return {'CANCELLED'}
        self.report({'INFO'}, rpt_("Camera placed — {view}").format(
            view=rpt_(_CAM_VIEW_LABELS.get(sp.camera_view, sp.camera_view))))
        return {'FINISHED'}


# Studio rig — (name, azimuth°, elevation°, distance×radius, size×radius, power×radius², color).
# Angles use the camera's convention (az 0 = front / -Y side, el = above the horizon).
# Area lights obey position, size and distance, so every number here is relative to the
# asset's bounding radius: power scales with radius² because irradiance falls off as 1/d²
# and d scales with radius, which keeps the exposure identical at any asset scale.
# Rim sits behind but curls back toward the camera's side: a back light directly opposite the
# camera hides its own lit crescent behind the asset. Verified against the default 3/4 view.
_LIGHT_RIGS = (
    ("KK_Key",   35.0, 35.0, 3.0, 2.0, 1.000, (1.00, 0.96, 0.90)),   # key: front upper-right, slightly warm
    ("KK_Fill", -55.0, 40.0, 3.5, 3.0, 0.325, (0.90, 0.95, 1.00)),   # fill: broad and soft from the opposite side
    ("KK_Rim",  195.0, 35.0, 3.0, 1.5, 0.750, (1.00, 1.00, 1.00)),   # rim: behind and above, lifts the silhouette
)

# Watts for the key light on a radius-1 asset. Calibrated (dev/calibrate_lights.py) against
# the key+fill of the Sun rig this replaces, so the switch to Area lights holds the exposure.
# Only key+fill: the old rim pointed at the front, and matching its light too would have the
# key overcompensate for a rim that now sits where it belongs, behind the asset.
LIGHT_KEY_POWER = 137.0


def fn_light_targets(context):
    """Meshes the lights should frame — the camera's targets, else every visible mesh."""
    targets = fn_camera_targets(context)
    if targets:
        return targets
    try:
        return [o for o in context.scene.objects if o.type == 'MESH' and o.visible_get()]
    except Exception:
        return [o for o in context.scene.objects if o.type == 'MESH']


class KILNKIT_OT_SetupStudioLights(bpy.types.Operator):
    bl_idname = "kilnkit.setup_studio_lights"; bl_label = "Set Up 3-Point Lights"; bl_options = {'REGISTER', 'UNDO'}
    bl_description = "Install key/fill/rim area lights around the selected asset, into the 'KK_Render_Lights' collection. Run again to replace"

    def execute(self, context):
        scene = context.scene
        coll = fn_clear_kk_lights()
        if not coll:
            coll = bpy.data.collections.new(KK_LIGHTS_COLL)
            scene.collection.children.link(coll)
        elif coll.name not in scene.collection.children:
            try:
                scene.collection.children.link(coll)
            except Exception:
                pass

        bb = fn_selected_bbox(fn_light_targets(context))
        if bb:
            center, size = bb
            radius = max(size.length / 2.0, 1e-4)
        else:   # nothing to frame — light the origin at a sane default scale
            center, radius = mathutils.Vector((0.0, 0.0, 0.0)), 1.0

        for nm, az, el, dist_f, size_f, power_f, col in _LIGHT_RIGS:
            ld = bpy.data.lights.new(nm, type='AREA')
            ld.shape = 'SQUARE'
            ld.size = max(size_f * radius, 1e-3)
            ld.energy = power_f * LIGHT_KEY_POWER * radius * radius
            ld.color = col
            ob = bpy.data.objects.new(nm, ld)
            ob.location = center + fn_orbit_direction(az, el) * (dist_f * radius)
            ob.rotation_euler = (center - ob.location).to_track_quat('-Z', 'Y').to_euler()
            coll.objects.link(ob)

        self.report({'INFO'}, rpt_("Studio 3-point lights installed (Key / Fill / Rim)"))
        return {'FINISHED'}


# ── Render settings / output / execution ──

_RES_PRESETS = {'512': 512, '1024': 1024, '2048': 2048, '4096': 4096}


def fn_set_engine(scene, choice):
    """Resolve the engine choice (EEVEE/CYCLES) to this build's actual enum id and apply it. The EEVEE id varies by version."""
    cands = ['CYCLES'] if choice == 'CYCLES' else ['BLENDER_EEVEE_NEXT', 'BLENDER_EEVEE']
    for c in cands:
        try:
            scene.render.engine = c
            return c
        except Exception:
            continue
    return scene.render.engine


def fn_capture_output_state(scene):
    """Snapshot the render-output settings Kilnkit temporarily changes (native filepath +
    image format), so they can be restored after a render — the user's Output settings and
    F12 target stay intact. Shared by RenderSave, multi-angle, turntable, and the queue."""
    img = scene.render.image_settings
    return {
        'filepath':    scene.render.filepath,
        'media_type':  getattr(img, 'media_type', None),
        'file_format': img.file_format,
        'color_mode':  img.color_mode,
    }


def fn_restore_output_state(scene, st):
    """Restore what fn_capture_output_state saved (back to the user's values, not defaults)."""
    try:
        img = scene.render.image_settings
        if st.get('media_type') is not None and hasattr(img, 'media_type'):
            img.media_type = st['media_type']   # restore first — switching media_type resets file_format
        img.file_format = st['file_format']
        img.color_mode = st['color_mode']
        scene.render.filepath = st['filepath']
    except Exception:
        pass


def _install_output_restore(scene, st):
    """Restore the output state once a non-blocking (INVOKE_DEFAULT) render finishes or
    cancels, then remove the handlers (one-shot, guarded to this scene). Render & Save's
    async render would otherwise leave the native filepath overwritten with the take path."""
    def _on_done(scn, *args):
        if scn is not scene:
            return
        fn_restore_output_state(scene, st)
        for hl in (bpy.app.handlers.render_complete, bpy.app.handlers.render_cancel):
            try:
                hl.remove(_on_done)
            except Exception:
                pass
    bpy.app.handlers.render_complete.append(_on_done)
    bpy.app.handlers.render_cancel.append(_on_done)


def fn_render_outdir(context):
    """Output folder = the directory of the native scene.render.filepath (the field shown in
    the render tab). // is relative to the .blend. An unsaved file whose path can't resolve
    (// relative) or is still Blender's default (/tmp) falls back to Home\\Kilnkit_Renders so
    first renders aren't lost in /tmp; a user-set absolute path is always honored. Kilnkit
    generates the file name itself, so only the directory part is used. Ensures the folder exists."""
    fp = context.scene.render.filepath or "//"
    saved = bool(bpy.data.filepath)
    home = os.path.join(os.path.expanduser("~"), "Kilnkit_Renders")
    is_default_tmp = fp.replace("\\", "/").rstrip("/") == "/tmp"
    if not saved and (fp.startswith("//") or is_default_tmp):
        ad = home
    else:
        folder = fp if fp.endswith(("/", "\\")) else (os.path.dirname(fp) or "//")
        ad = bpy.path.abspath(folder) or home        # // that still can't resolve → home
    try:
        os.makedirs(ad, exist_ok=True)
    except Exception:
        pass
    return ad


def fn_render_basename(context):
    """Render file base name — asset name (naming_base) > active/selected mesh name > 'render'."""
    sp = context.scene.kilnkit_scene_props
    if sp.naming_base.strip():
        return sp.naming_base.strip()
    obj = context.active_object
    if obj and obj.type == 'MESH':
        return obj.name
    sels = [o for o in context.selected_objects if o.type == 'MESH']
    if sels:
        return sels[0].name
    return "render"


def fn_setup_png_output(scene):
    """PNG output setup — RGBA (alpha channel) when the background is transparent
    (film_transparent), RGB otherwise. Prevents the bug where film_transparent alone
    with RGB output saved no alpha and produced a black background."""
    img = scene.render.image_settings
    if hasattr(img, 'media_type'):      # Blender 4.4+ — leave VIDEO mode so PNG is selectable
        img.media_type = 'IMAGE'
    img.file_format = 'PNG'
    img.color_mode = 'RGBA' if scene.render.film_transparent else 'RGB'


def fn_render_target_path(outdir, basename, mode, ext=".png"):
    """Final render path per exist-mode (extension-less — Blender appends the extension).
    No collision → unchanged regardless of mode. NUMBER = first free _001.._999,
    OVERWRITE = same path, SKIP = None (skipped). Unlike Blender frame numbers
    (separator-less 0001), the number is an _NNN take counter."""
    base = os.path.join(outdir, basename)
    if not os.path.exists(base + ext):
        return base
    if mode == 'OVERWRITE':
        return base
    if mode == 'SKIP':
        return None
    for i in range(1, 1000):               # NUMBER (default) — find a free number
        cand = f"{base}_{i:03d}"
        if not os.path.exists(cand + ext):
            return cand
    return base                            # fallback: overwrite past 999


def fn_iter_fcurves(action):
    """Iterate an action's F-curves across legacy and slotted (Blender 4.4+) actions.
    Blender 4.4 removed Action.fcurves in favor of layers → strips → channelbags → fcurves."""
    if action is None:
        return
    legacy = getattr(action, "fcurves", None)
    if legacy is not None:
        for fc in legacy:
            yield fc
        return
    for layer in getattr(action, "layers", []):
        for strip in getattr(layer, "strips", []):
            for cb in getattr(strip, "channelbags", []):
                for fc in getattr(cb, "fcurves", []):
                    yield fc


_FFMPEG_OK = None   # cached FFmpeg-video availability (probed once per session)


def fn_ffmpeg_known_missing():
    """True only once a probe has actually run and found no FFmpeg. Safe to call from draw()."""
    return _FFMPEG_OK is False


def fn_ffmpeg_probe():
    """One-shot timer body — resolve FFmpeg availability in a context that allows writes."""
    if bpy.data.scenes:
        fn_ffmpeg_available()
    return None


def fn_ffmpeg_available(scene=None):
    """True when this Blender build can actually write FFmpeg video (some builds ship without
    it → MP4 is impossible). bpy.app.build_options.codec_ffmpeg can be True while the FFMPEG
    output format is still unavailable at runtime, so we probe the real enum once (set +
    restore) and cache it.

    ⚠ Probing *writes* to the scene, and Blender forbids ID writes from draw(). Treating that
    refusal as "no FFmpeg" would cache a permanent lie: the panel would claim the build has no
    FFmpeg and every turntable would fall back to a PNG sequence. So a refusal is answered
    optimistically and left uncached — draw() reads fn_ffmpeg_known_missing() instead, and the
    one-shot fn_ffmpeg_probe() timer fills the cache from a context where writing is allowed."""
    global _FFMPEG_OK
    if _FFMPEG_OK is None:
        scn = scene or (bpy.data.scenes[0] if bpy.data.scenes else None)
        if scn is None:
            return True                           # can't probe yet — assume yes, don't cache
        img = scn.render.image_settings
        # Blender 4.4+ gates video formats behind media_type='VIDEO' — assigning FFMPEG while
        # media_type is IMAGE is rejected, so switch first (then probe).
        cur_media = getattr(img, 'media_type', None)
        cur_fmt = img.file_format
        probed = None
        try:
            if cur_media is not None:
                img.media_type = 'VIDEO'
            img.file_format = 'FFMPEG'
            probed = True
        except AttributeError:
            probed = None                         # write refused by the context — nothing happened
        except Exception:
            probed = False                        # the enum really has no FFMPEG
        finally:
            if cur_media is not None:             # restore media_type first (resets file_format)
                try:
                    img.media_type = cur_media
                except Exception:
                    pass
            try:
                img.file_format = cur_fmt
            except Exception:
                pass
        if probed is None:
            return True
        _FFMPEG_OK = probed
    return _FFMPEG_OK


def fn_setup_video_output(scene, fmt):
    """Set the render output for a turntable — FFmpeg H.264 MP4, or a PNG image sequence
    (RGBA when the background is transparent, like the stills)."""
    if fmt == 'PNG_SEQUENCE':
        fn_setup_png_output(scene)
        return
    img = scene.render.image_settings
    if hasattr(img, 'media_type'):         # Blender 4.4+ — required before FFMPEG is selectable
        img.media_type = 'VIDEO'
    img.file_format = 'FFMPEG'
    ff = scene.render.ffmpeg
    ff.format = 'MPEG4'                     # → .mp4 container
    ff.codec = 'H264'
    ff.constant_rate_factor = 'HIGH'       # visually lossless-ish, reasonable size
    ff.ffmpeg_preset = 'GOOD'
    ff.audio_codec = 'NONE'


def fn_setup_turntable(context, targets, frames, sp):
    """Build a camera-orbit turntable: an empty KK_Turntable_Pivot at the bbox center with
    KK_Camera parented to it (kept in place), and the pivot's Z rotation keyframed 0→360°.
    Keyframes sit at frame 1 (0°) and frame frames+1 (360°) so rendering 1..frames is a
    seamless loop. Places KK_Camera first when there's no scene camera. Lights/world stay
    fixed (product-turntable standard). Returns the pivot (None on failure)."""
    bb = fn_selected_bbox(targets)
    if not bb:
        return None
    center, _size = bb
    cam = context.scene.camera
    if not cam or cam.type != 'CAMERA':
        cam = fn_place_camera(context, targets, sp.camera_view, sp.camera_lens, sp.camera_margin)
        if not cam:
            return None
    # Pivot empty at the bbox center — fixed name, cleared and reused on re-run
    piv = bpy.data.objects.get(KK_TURNTABLE_PIVOT)
    if piv is None or piv.type != 'EMPTY':
        piv = bpy.data.objects.new(KK_TURNTABLE_PIVOT, None)
        piv.empty_display_type = 'PLAIN_AXES'
    if context.scene.objects.get(piv.name) is None:
        try:
            context.scene.collection.objects.link(piv)
        except Exception:
            pass
    piv.animation_data_clear()
    piv.location = center
    piv.rotation_euler = (0.0, 0.0, 0.0)
    # Parent the camera to the pivot without moving it in world space
    context.view_layer.update()
    cam.parent = piv
    cam.matrix_parent_inverse = piv.matrix_world.inverted()
    # Full turn — 0° at frame 1, 360° at frame frames+1 (1..frames loops seamlessly)
    piv.rotation_euler.z = 0.0
    piv.keyframe_insert("rotation_euler", index=2, frame=1)
    piv.rotation_euler.z = 2.0 * math.pi
    piv.keyframe_insert("rotation_euler", index=2, frame=frames + 1)
    piv.rotation_euler.z = 0.0
    if piv.animation_data and piv.animation_data.action:   # constant angular velocity
        for fc in fn_iter_fcurves(piv.animation_data.action):
            for kp in fc.keyframe_points:
                kp.interpolation = 'LINEAR'
    return piv


def fn_turntable_render_path(outdir, base, fmt, mode):
    """(render_filepath, resolved_stem) for a turntable, or (None, None) when skipped.
    Clean output name <base>_turntable with collision handling (NUMBER default → _001…,
    never silently overwrites). MP4: render writes <stem><framerange>.mp4, renamed to
    <stem>.mp4 afterwards. PNG sequence: Blender appends 0001.png to the returned filepath."""
    stem = f"{base}_turntable"
    if fmt == 'PNG_SEQUENCE':
        resolved = fn_render_target_path(outdir, stem, mode, ext="_0001.png")
        if resolved is None:
            return None, None
        return resolved + "_", resolved            # → <resolved>_0001.png per frame
    resolved = fn_render_target_path(outdir, stem, mode, ext=".mp4")
    if resolved is None:
        return None, None
    return resolved, resolved                      # video renamed to <resolved>.mp4


def fn_finalize_turntable_video(resolved):
    """Rename Blender's <stem><framerange>.mp4 to the clean <stem>.mp4. Returns the final path
    (None if nothing was produced)."""
    final = resolved + ".mp4"
    if os.path.exists(final):
        return final
    outdir = os.path.dirname(resolved)
    stem = os.path.basename(resolved)
    try:
        cands = [f for f in os.listdir(outdir)
                 if f.startswith(stem) and f.lower().endswith(".mp4")]
    except Exception:
        return None
    if not cands:
        return None
    cands.sort(key=lambda f: os.path.getmtime(os.path.join(outdir, f)), reverse=True)
    src = os.path.join(outdir, cands[0])           # newest match = the one just rendered
    try:
        os.replace(src, final)
        return final
    except Exception:
        return src


class KILNKIT_OT_ApplyRenderSettings(bpy.types.Operator):
    bl_idname = "kilnkit.apply_render_settings"; bl_label = "Apply Render Settings"; bl_options = {'REGISTER', 'UNDO'}
    bl_description = "Apply engine, resolution, and samples. Light defaults (low viewport samples) + GPU for Cycles when available"

    def execute(self, context):
        sp = context.scene.kilnkit_scene_props
        scene = context.scene
        eng = fn_set_engine(scene, sp.render_engine_choice)
        res = _RES_PRESETS.get(sp.render_res, 1024)
        scene.render.resolution_x = res
        scene.render.resolution_y = res
        scene.render.resolution_percentage = 100

        msg_extra = ""
        if eng == 'CYCLES':
            scene.cycles.samples = sp.render_samples
            scene.cycles.preview_samples = min(64, sp.render_samples)   # light viewport
            try:
                cprefs = context.preferences.addons['cycles'].preferences
                cur = getattr(cprefs, 'compute_device_type', 'NONE')
                if cur and cur != 'NONE':
                    cprefs.get_devices()
                    if any(d.type == cur for d in cprefs.devices):
                        for d in cprefs.devices:
                            d.use = (d.type != 'CPU')
                        scene.cycles.device = 'GPU'
                        msg_extra = f" · GPU({cur})"
            except Exception:
                pass
        else:
            try:
                scene.eevee.taa_render_samples = sp.render_samples
                scene.eevee.taa_samples = min(16, sp.render_samples)    # light viewport
            except Exception:
                pass

        self.report({'INFO'}, rpt_("Render settings applied — {eng}, {res}px, {s} samples{extra}").format(
            eng=eng, res=res, s=sp.render_samples, extra=msg_extra))
        return {'FINISHED'}


class KILNKIT_OT_RenderSave(bpy.types.Operator):
    bl_idname = "kilnkit.render_save"; bl_label = "Render & Save"; bl_options = {'REGISTER'}
    bl_description = "Render through the scene camera and save a PNG to the output folder (file name = asset name_angle)"

    def execute(self, context):
        scene = context.scene
        if not scene.camera:
            self.report({'ERROR'}, rpt_("No scene camera — press 'Auto-Place Camera' first"))
            return {'CANCELLED'}
        sp = scene.kilnkit_scene_props
        outdir = fn_render_outdir(context)
        fname = f"{fn_render_basename(context)}_{sp.camera_view.lower()}"
        target = fn_render_target_path(outdir, fname, sp.render_exist_mode)
        if target is None:
            self.report({'WARNING'}, rpt_("Already exists, skipped: {name}.png (If File Exists = Skip)").format(name=fname))
            return {'CANCELLED'}
        out_state = fn_capture_output_state(scene)      # remember the user's Output settings
        fn_setup_png_output(scene)
        scene.render.filepath = target
        savename = os.path.basename(target)
        try:
            if bpy.app.background:
                bpy.ops.render.render(write_still=True)            # headless: synchronous
                fn_restore_output_state(scene, out_state)          # restore right after
                self.report({'INFO'}, rpt_("Render saved: {name}.png → {dir}").format(name=savename, dir=outdir))
            else:
                res = bpy.ops.render.render('INVOKE_DEFAULT', write_still=True)  # GUI: render window + progress, non-blocking
                if 'RUNNING_MODAL' in res:
                    _install_output_restore(scene, out_state)      # restore when the async render finishes
                    self.report({'INFO'}, rpt_("Rendering — will save when done: {name}.png → {dir}").format(name=savename, dir=outdir))
                else:
                    fn_restore_output_state(scene, out_state)      # finished synchronously / did not start
                    self.report({'INFO'}, rpt_("Render saved: {name}.png → {dir}").format(name=savename, dir=outdir))
        except Exception as e:
            fn_restore_output_state(scene, out_state)              # restore on failure too
            self.report({'ERROR'}, rpt_("Render failed: {err}").format(err=e))
            return {'CANCELLED'}
        return {'FINISHED'}


# ================================================================
# Non-blocking sequential render engine
#   Shared heart for multi-angle now and the render queue (paid) later. A modal-operator
#   mixin that renders a job list one at a time via INVOKE_DEFAULT (render window +
#   progress bar) and advances on the render_complete handler, so the UI stays responsive
#   and Esc cancels. Each render is KICKED FROM A MODAL TIMER EVENT (not a bare app timer):
#   starting a render from an app timer is unreliable — Blender may refuse it while the
#   previous render is still tearing down, and the refusal ({'CANCELLED'}) is silent, which
#   stalls the sequence. So we check INVOKE's return value and re-try the same job on a
#   later tick until it starts (bounded by _SQ_MAX_RETRY). Headless (bpy.app.background)
#   has no event loop → callers render with a synchronous loop instead.
#
#   A job is a dict {'label': str, 'prepare': callable(scene) -> target_or_None}. prepare()
#   sets up the scene (camera, etc.) and returns the extension-less target path, or None to
#   skip. An optional 'animation': True renders the whole frame range (INVOKE animation=True,
#   render_complete fires once) instead of a single still — the job's prepare() is then
#   responsible for the output format (video / PNG sequence). Subclasses build jobs in
#   execute() and return self._start_sequence(context, jobs), and implement
#   _on_sequence_done(saved, skipped, cancelled).
# ================================================================

class _KILNKIT_RenderSequence:
    _seq_label = "Rendering"
    _SQ_MAX_RETRY = 25                # ~5 s of 0.2 s ticks before giving up on a stuck start
    _running = False                  # guards against two sequences sharing the render window

    def _start_sequence(self, context, jobs):
        self._sq_jobs = list(jobs)
        self._sq_scene = context.scene
        self._sq_index = 0
        self._sq_saved = []
        self._sq_skipped = []
        self._sq_pending = None
        self._sq_rendering = False
        self._sq_cancelled = False
        self._sq_done = False
        self._sq_retry = 0
        _KILNKIT_RenderSequence._running = True
        bpy.app.handlers.render_complete.append(self._sq_on_complete)
        bpy.app.handlers.render_cancel.append(self._sq_on_cancel)
        wm = context.window_manager
        self._sq_timer = wm.event_timer_add(0.2, window=context.window)
        wm.modal_handler_add(self)
        self._sq_set_status(context)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if self._sq_done:
            return {'FINISHED'}
        if (self._sq_cancelled or event.type == 'ESC') and not self._sq_rendering:
            return self._sq_finish(context, True)
        if event.type != 'TIMER' or self._sq_rendering:
            return {'PASS_THROUGH'}               # waiting on a render or a non-timer event
        if self._sq_index >= len(self._sq_jobs):
            return self._sq_finish(context, False)
        self._sq_kick(context)
        return {'PASS_THROUGH'}

    def _sq_kick(self, context):
        """Prepare + start the current job (from a modal TIMER). Skips chain inline; a job
        whose INVOKE didn't start is retried on later ticks (index unchanged)."""
        while self._sq_index < len(self._sq_jobs):
            job = self._sq_jobs[self._sq_index]
            self._sq_set_status(context)
            try:
                target = job['prepare'](self._sq_scene)
            except Exception:
                target = None
            if target is None:                    # already exists / prepare failed → skip
                self._sq_skipped.append(job.get('label', str(self._sq_index)))
                self._sq_index += 1
                self._sq_retry = 0
                continue
            self._sq_scene.render.filepath = target
            anim = job.get('animation', False)
            if not anim:
                fn_setup_png_output(self._sq_scene)   # animation jobs set their own output in prepare()
            self._sq_pending = job
            self._sq_rendering = True
            try:
                if anim:
                    res = bpy.ops.render.render('INVOKE_DEFAULT', animation=True)
                else:
                    res = bpy.ops.render.render('INVOKE_DEFAULT', write_still=True)
            except Exception:
                res = {'CANCELLED'}
            if 'RUNNING_MODAL' in res or 'FINISHED' in res:
                self._sq_retry = 0
                return                            # started — wait for render_complete
            # Not started (Blender still busy from the previous render) — retry this job on
            # a later tick; give up after _SQ_MAX_RETRY so we never wait forever.
            self._sq_pending = None
            self._sq_rendering = False
            self._sq_retry += 1
            if self._sq_retry <= self._SQ_MAX_RETRY:
                return                            # retry next TIMER (index unchanged)
            self._sq_skipped.append(job.get('label', str(self._sq_index)))
            self._sq_index += 1
            self._sq_retry = 0
        # nothing left to kick — modal finishes on the next tick

    def _sq_on_complete(self, scene, *args):
        if scene is not self._sq_scene or self._sq_done:
            return
        if self._sq_pending is not None:
            self._sq_saved.append(self._sq_pending.get('label', str(self._sq_index)))
            self._sq_pending = None
        self._sq_index += 1
        self._sq_retry = 0
        self._sq_rendering = False                # modal TIMER kicks the next job

    def _sq_on_cancel(self, scene, *args):
        if scene is not self._sq_scene or self._sq_done:
            return
        self._sq_pending = None
        self._sq_rendering = False
        self._sq_cancelled = True                 # abort the whole sequence

    def _sq_set_status(self, context):
        try:
            n = len(self._sq_jobs)
            i = min(self._sq_index + 1, n)
            context.workspace.status_text_set(
                iface_("{label} {i}/{n}…  (Esc to cancel)").format(
                    label=iface_(self._seq_label), i=i, n=n))
        except Exception:
            pass

    def _sq_finish(self, context, cancelled):
        if not self._sq_done:
            self._sq_done = True
            for hlist, fn in ((bpy.app.handlers.render_complete, self._sq_on_complete),
                              (bpy.app.handlers.render_cancel, self._sq_on_cancel)):
                try:
                    hlist.remove(fn)
                except Exception:
                    pass
            try:
                context.window_manager.event_timer_remove(self._sq_timer)
            except Exception:
                pass
            try:
                context.workspace.status_text_set(None)
            except Exception:
                pass
            _KILNKIT_RenderSequence._running = False
            try:
                self._on_sequence_done(self._sq_saved, self._sq_skipped, cancelled)
            except Exception:
                pass
        return {'FINISHED'}

    def cancel(self, context):
        self._sq_finish(context, True)


class KILNKIT_OT_RenderMultiAngle(_KILNKIT_RenderSequence, bpy.types.Operator):
    bl_idname = "kilnkit.render_multi_angle"; bl_label = "Render 4 Multi-Angles"; bl_options = {'REGISTER'}
    bl_description = "Render front, 3/4, side, and top views to PNG (file name = asset name_angle). One angle at a time without blocking — the render window steps through them, Esc cancels"
    _seq_label = "Rendering angle"
    _VIEWS = ('FRONT', 'THREEQ', 'SIDE', 'TOP')

    @staticmethod
    def _result(outdir, saved, skipped, cancelled):
        """(report level, message) for a finished multi-angle run — shared by the headless
        (synchronous) and GUI (async) paths."""
        n, ns = len(saved), len(skipped)
        if not n and not ns:
            return {'ERROR'}, rpt_("Render save failed")
        skip_msg = rpt_(" · {n} skipped").format(n=ns) if ns else ""
        if cancelled:
            return {'WARNING'}, rpt_("Cancelled — saved {n}{skip} → {dir}").format(n=n, skip=skip_msg, dir=outdir)
        return {'INFO'}, rpt_("Multi-angle: saved {n}{skip} → {dir}").format(n=n, skip=skip_msg, dir=outdir)

    def execute(self, context):
        targets = fn_camera_targets(context)
        if not targets:
            self.report({'ERROR'}, rpt_("Select a mesh object")); return {'CANCELLED'}
        scene = context.scene
        sp = scene.kilnkit_scene_props
        outdir = fn_render_outdir(context)
        base = fn_render_basename(context)
        self._ma_state = fn_capture_output_state(scene)   # restore filepath/format after the run

        # One job per angle — prepare() places KK_Camera for the angle and returns the
        # target path (None = already exists → skip). fn_place_camera uses the live
        # bpy.context because the passed context is stale by the time async jobs run.
        def make_prepare(view):
            def _prepare(scn):
                target = fn_render_target_path(outdir, f"{base}_{view.lower()}", sp.render_exist_mode)
                if target is None:
                    return None
                if not fn_place_camera(bpy.context, targets, view, sp.camera_lens, sp.camera_margin):
                    return None
                return target
            return _prepare

        jobs = [{'label': v.lower(), 'prepare': make_prepare(v)} for v in self._VIEWS]

        # Headless — no event loop, render synchronously (unchanged behavior)
        if bpy.app.background or context.window is None:
            saved, skipped = [], []
            for job in jobs:
                target = job['prepare'](scene)
                if target is None:
                    skipped.append(job['label']); continue
                scene.render.filepath = target
                fn_setup_png_output(scene)
                try:
                    bpy.ops.render.render(write_still=True)
                    saved.append(job['label'])
                except Exception:
                    pass
            fn_restore_output_state(scene, self._ma_state)
            level, msg = self._result(outdir, saved, skipped, False)
            self.report(level, msg)
            return {'CANCELLED'} if 'ERROR' in level else {'FINISHED'}

        # GUI — modal non-blocking sequence
        if _KILNKIT_RenderSequence._running:
            self.report({'WARNING'}, rpt_("A render is already running — try again when it finishes"))
            return {'CANCELLED'}
        self._ma_outdir = outdir
        self.report({'INFO'}, rpt_("Rendering 4 angles — the render window will step through them"))
        return self._start_sequence(context, jobs)

    def _on_sequence_done(self, saved, skipped, cancelled):
        fn_restore_output_state(self._sq_scene, self._ma_state)   # user's Output settings back
        level, msg = self._result(self._ma_outdir, saved, skipped, cancelled)
        self.report(level, msg)                   # runs from modal → still a valid operator context
        print("[Kilnkit] " + msg)


class KILNKIT_OT_RenderTurntable(_KILNKIT_RenderSequence, bpy.types.Operator):
    bl_idname = "kilnkit.render_turntable"; bl_label = "Render Turntable"; bl_options = {'REGISTER'}
    bl_description = ("Orbit the camera 360° around the selected asset and render a spinning turntable "
                     "(MP4 video or PNG sequence). Non-blocking — the render window shows progress, Esc cancels")
    _seq_label = "Rendering turntable"

    def _capture(self, scene):
        """Snapshot the scene state the turntable changes, to restore when the render finishes.
        Output state (filepath/format) via the shared helper + the frame range/fps on top."""
        st = fn_capture_output_state(scene)
        st.update({'frame_start': scene.frame_start, 'frame_end': scene.frame_end,
                   'fps': scene.render.fps, 'fps_base': scene.render.fps_base})
        return st

    def _restore(self, scene, st):
        try:
            scene.frame_start = st['frame_start']; scene.frame_end = st['frame_end']
            scene.render.fps = st['fps']; scene.render.fps_base = st['fps_base']
        except Exception:
            pass
        fn_restore_output_state(scene, st)   # image_settings + filepath (media_type-first ordering)

    def _note(self):
        return rpt_(" (no FFmpeg in this Blender — saved as a PNG sequence)") if self._tt_fell_back else ""

    def _finalize(self, ok):
        """Clean up outputs and return the display file name."""
        if self._tt_fmt == 'PNG_SEQUENCE':
            return os.path.basename(self._tt_final) + "_####.png"
        if ok:                                    # MP4 — rename <stem><framerange>.mp4 → <stem>.mp4
            final = fn_finalize_turntable_video(self._tt_render_fp)
            if final:
                return os.path.basename(final)
        return os.path.basename(self._tt_final) + ".mp4"

    def execute(self, context):
        targets = fn_camera_targets(context)
        if not targets:
            self.report({'ERROR'}, rpt_("Select a mesh object")); return {'CANCELLED'}
        scene = context.scene
        sp = scene.kilnkit_scene_props
        outdir = fn_render_outdir(context)
        base = fn_render_basename(context)
        frames = sp.turntable_frames
        # MP4 needs FFmpeg — fall back to a PNG sequence on builds without it (no crash)
        fmt = sp.turntable_format
        self._tt_fell_back = (fmt == 'MP4' and not fn_ffmpeg_available(scene))
        if self._tt_fell_back:
            fmt = 'PNG_SEQUENCE'

        render_fp, final = fn_turntable_render_path(outdir, base, fmt, sp.render_exist_mode)
        if render_fp is None:
            self.report({'WARNING'}, rpt_("Already exists, skipped (If File Exists = Skip)"))
            return {'CANCELLED'}
        if fn_setup_turntable(context, targets, frames, sp) is None:
            self.report({'ERROR'}, rpt_("Cannot compute a bounding box")); return {'CANCELLED'}

        st = self._capture(scene)
        scene.frame_start = 1
        scene.frame_end = frames
        scene.render.fps = sp.turntable_fps
        scene.render.fps_base = 1.0
        self._tt_state, self._tt_fmt = st, fmt
        self._tt_render_fp, self._tt_final, self._tt_outdir = render_fp, final, outdir

        # Headless — no event loop, render synchronously
        if bpy.app.background or context.window is None:
            fn_setup_video_output(scene, fmt)
            scene.render.filepath = render_fp
            ok = False
            try:
                bpy.ops.render.render(animation=True)
                ok = True
            except Exception as e:
                self.report({'ERROR'}, rpt_("Render failed: {err}").format(err=e))
            name = self._finalize(ok)
            self._restore(scene, st)
            if ok:
                self.report({'INFO'}, rpt_("Turntable saved: {name} → {dir}{note}").format(
                    name=name, dir=outdir, note=self._note()))
                return {'FINISHED'}
            return {'CANCELLED'}

        # GUI — one animation job on the shared non-blocking engine
        if _KILNKIT_RenderSequence._running:
            self.report({'WARNING'}, rpt_("A render is already running — try again when it finishes"))
            self._restore(scene, st)
            return {'CANCELLED'}

        def _prepare(scn):
            fn_setup_video_output(scn, fmt)       # engine sets filepath + starts animation render
            return render_fp

        jobs = [{'label': 'turntable', 'prepare': _prepare, 'animation': True}]
        self.report({'INFO'}, rpt_("Rendering turntable — {n} frames, the render window shows progress").format(n=frames))
        return self._start_sequence(context, jobs)

    def _on_sequence_done(self, saved, skipped, cancelled):
        ok = bool(saved) and not cancelled
        name = self._finalize(ok)
        self._restore(self._sq_scene, self._tt_state)
        if cancelled:
            level, msg = {'WARNING'}, rpt_("Turntable cancelled")
        elif ok:
            level, msg = {'INFO'}, rpt_("Turntable saved: {name} → {dir}{note}").format(
                name=name, dir=self._tt_outdir, note=self._note())
        else:
            level, msg = {'ERROR'}, rpt_("Turntable render failed")
        self.report(level, msg)
        print("[Kilnkit] " + msg)


def _sync_timer():
    """While auto_sync is on, sync the active slot with node values every 0.5 s."""
    try:
        ctx   = bpy.context
        scene = getattr(ctx, "scene", None)
        sp    = getattr(scene, "kilnkit_scene_props", None) if scene else None
        if sp and sp.auto_sync:
            sync_active_slot_from_nodes(ctx)
    except Exception:
        pass  # keep the timer alive through transient missing-context states
    return 0.5


# ================================================================
# Registration
# ================================================================

classes = (
    KILNKIT_OT_AddSlot,
    KILNKIT_OT_ImportSubfolders,
    KILNKIT_OT_RemoveSlot,
    KILNKIT_OT_MoveSlot,
    KILNKIT_OT_OneClick,
    KILNKIT_OT_RebuildPBR,
    KILNKIT_OT_ReapplyUV,
    KILNKIT_OT_FinishUV,
    KILNKIT_OT_StepScale,
    KILNKIT_OT_StepUV,
    KILNKIT_OT_StepPBR,
    KILNKIT_OT_Assign,
    KILNKIT_OT_ToggleAsset,
    KILNKIT_OT_LibBuild,
    KILNKIT_OT_LibBuildSubfolders,
    KILNKIT_OT_LibRebuild,
    KILNKIT_OT_LibRemove,
    KILNKIT_OT_LibAssign,
    KILNKIT_OT_SlotFromLibrary,
    KILNKIT_OT_SlotsFromObject,
    KILNKIT_OT_MakeSingleUser,
    KILNKIT_OT_LibExport,
    KILNKIT_OT_OpenAssetBrowser,
    KILNKIT_OT_Decimate,
    KILNKIT_OT_GenerateLOD,
    KILNKIT_OT_BatchRun,
    KILNKIT_OT_ApplyNaming,
    KILNKIT_OT_RemoveNumberSuffix,
    KILNKIT_FH_TextureDrop,
    KILNKIT_OT_DropTexture,
    KILNKIT_OT_CleanupImages,
    KILNKIT_OT_DedupMaterials,
    KILNKIT_OT_ResetSuffix,
    KILNKIT_OT_ResetSettings,
    KILNKIT_OT_SyncFromNodes,
    KILNKIT_OT_SetupEnvironment,
    KILNKIT_OT_SetupStudioLights,
    KILNKIT_OT_SetupCamera,
    KILNKIT_OT_ApplyRenderSettings,
    KILNKIT_OT_RenderSave,
    KILNKIT_OT_RenderMultiAngle,
    KILNKIT_OT_RenderTurntable,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    if not bpy.app.timers.is_registered(_sync_timer):
        bpy.app.timers.register(_sync_timer, first_interval=0.5, persistent=True)
    # Probe FFmpeg off a timer, not from draw() — draw cannot write to the scene, and a
    # refused write there used to be cached as "this build has no FFmpeg".
    if not bpy.app.timers.is_registered(fn_ffmpeg_probe):
        bpy.app.timers.register(fn_ffmpeg_probe, first_interval=0.0)


def unregister():
    # Defensively strip any lingering render-sequence handlers (e.g. reload mid-render)
    _KILNKIT_RenderSequence._running = False
    for name in ("render_complete", "render_cancel"):
        hlist = getattr(bpy.app.handlers, name)
        for h in list(hlist):
            qn = getattr(h, "__qualname__", "")
            if qn.startswith("_KILNKIT_RenderSequence") or "_install_output_restore" in qn:
                try:
                    hlist.remove(h)
                except Exception:
                    pass
    if bpy.app.timers.is_registered(_sync_timer):
        bpy.app.timers.unregister(_sync_timer)
    if bpy.app.timers.is_registered(fn_ffmpeg_probe):   # still pending if unregistered fast
        bpy.app.timers.unregister(fn_ffmpeg_probe)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
