import bpy
import os

try:
    from . import render_queue          # paid module — absent in the lite build
except Exception:
    render_queue = None

try:
    from .edition import EDITION         # "full" (repo default) or "lite" (build script)
except Exception:
    EDITION = "full"

from .props import ADDON_VERSION
from .operators import (
    fn_scan_textures, fn_get_basecolor_image, fn_naming_final,
    fn_slot_folder_synced, fn_first_slot_dir, fn_find_sidecar,
    fn_naming_base, fn_naming_material_name, fn_slot_folder_name,
    fn_ffmpeg_known_missing, fn_uv_status, fn_mesh_has_uv,
)
from .props import UV_UNWRAP_METHODS, UV_OBJECT_COORD_METHODS

# Static text= strings are auto-translated by Blender at draw time, but dynamic
# compositions (f-strings etc.) never match the catalog → translate the template
# with iface_ first, then fill it with format.
from bpy.app.translations import pgettext_iface as iface_

# {abs_dir_path: (mtime, found_dict, sidecar_name)} — avoids repeating os.listdir
# on every UI redraw. sidecar_name: .mtlx filename (if any) / None — source label.
_tex_cache: dict = {}

# UV mapping methods — short name (Main tab) / one-line purpose (always shown in Settings)
_MAP_METHOD_SHORT = {
    'TRIPLANAR': "Triplanar", 'OBJECT': "Object Coords", 'KEEP': "Existing UV",
    'CUBE': "Cube Projection", 'UV': "Smart UV", 'SLIM': "SLIM Unwrap",
}
_MAP_METHOD_DESC = {
    'TRIPLANAR': "Tiling textures — uniform on any shape, no seams",
    'OBJECT':    "Seamless textures — flat projection (sides may stretch)",
    'KEEP':      "Imported assets — reuse the UV map the mesh already has",
    'CUBE':      "Box-shaped objects — six-sided box projection",
    'UV':        "Baking / unique textures — auto seams, then unwrap",
    'SLIM':      "High-quality unwrap — auto seams + minimum stretch",
}

# One-line UV status shown on the Main tab — the silent trap made visible.
# Keys come from operators.fn_uv_status().
_UV_STATUS = {
    'BROKEN':    ("No UV map — material samples UV coords", 'ERROR'),
    'NO_UV':     ("No UV map — preview only. Baking and export need one", 'ERROR'),
    'IGNORED':   ("Existing UV map is not being used (preview)", 'INFO'),
    'KEPT':      ("Using the mesh's existing UV map", 'CHECKMARK'),
    'GENERATED': ("UV map created by Kilnkit", 'CHECKMARK'),
}

# Add-on-specific translation context — when Blender's core catalog owns the same
# word (e.g. "Col", "Rough", "Lighting"), core translations win, so short/common
# words are looked up under this context to guarantee our translation is used.
_I18N_CTX = "Kilnkit"

# Channel abbreviations for the detected/missing labels — full names get clipped in
# a narrow panel ("basecolor, r..."). Translated per item with iface_ at draw time
# (the joined string can't live in the catalog).
_CH_ABBR = {
    'basecolor': "Col", 'normal': "Nrm", 'roughness': "Rough",
    'height': "Hgt", 'ao': "AO", 'metallic': "Metal",
}
def _ch_join(chs):
    return " · ".join(iface_(_CH_ABBR.get(c, c), _I18N_CTX) for c in chs)


# ================================================================
# UIList — slot list
# ================================================================

class KILNKIT_UL_SlotList(bpy.types.UIList):
    bl_idname = "KILNKIT_UL_slot_list"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname):
        obj = data
        try:
            slot_index = list(obj.kilnkit_slots).index(item)
        except ValueError:
            return
        has_mat = slot_index < len(obj.data.materials) and obj.data.materials[slot_index]

        row = layout.row(align=True)
        row.label(
            text=f"Slot {slot_index+1}",
            icon='CHECKMARK' if has_mat else 'LAYER_USED'
        )
        if has_mat:
            row.label(text=obj.data.materials[slot_index].name)
        elif item.directory:
            folder = os.path.basename(os.path.normpath(bpy.path.abspath(item.directory)))
            row.label(text=folder or "Folder set")
        else:
            row.label(text="No folder")


def _mat_icon_id(mat):
    """Material thumbnail icon_id — asset sphere preview (same as the Asset Browser) first, else 0."""
    try:
        mat.preview_ensure()
        if mat.preview and mat.preview.icon_id:
            return mat.preview.icon_id
    except Exception:
        pass
    return 0


class KILNKIT_UL_LibList(bpy.types.UIList):
    """Library material list — kilnkit_lib-tagged only; preview icon + name (scrollable)."""
    bl_idname = "KILNKIT_UL_lib_list"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname):
        iid = _mat_icon_id(item)
        if iid:
            layout.label(text=item.name, icon_value=iid)
        else:
            layout.label(text=item.name, icon='MATERIAL')

    def filter_items(self, context, data, propname):
        mats = getattr(data, propname)
        needle = self.filter_name.lower()
        flags = []
        for m in mats:
            ok = bool(m.get("kilnkit_lib"))
            if ok and needle and needle not in m.name.lower():
                ok = False
            flags.append(self.bitflag_filter_item if ok else 0)
        return flags, []


# ================================================================
# Helpers
# ================================================================

# Tab id → (icon, display name). Five text tabs get clipped in a narrow N panel,
# so: icon tabs + one line below with the current tab name (no icon-only guessing).
_TAB_META = {
    'MAIN':     ('MATERIAL',      "Main"),
    'SETTINGS': ('PREFERENCES',   "Settings"),
    'BATCH':    ('MOD_ARRAY',     "Batch"),
    'LIBRARY':  ('ASSET_MANAGER', "Library"),
    'RENDER':   ('RENDER_STILL',  "Render"),
}


def _draw_tab_bar(layout, sp):
    row = layout.row(align=True)
    row.scale_y = 1.2   # full-width tab bar slightly lower (1.5→1.2); icon glyphs are fixed-size → only button height changes
    for tab_id, (icon, _name) in _TAB_META.items():
        row.prop_enum(sp, "active_tab", tab_id, text="", icon=icon)
    cur = _TAB_META.get(sp.active_tab)
    if cur:
        layout.label(text="▍ " + iface_(cur[1]), icon=cur[0])
    layout.separator()


def _section(layout, sp, label, prop_name):
    """Toggle section header — returns the box (open or closed).
    The label is pre-translated in the add-on context (avoids core-catalog
    collisions — e.g. core turns "Lighting" into a different Korean word)."""
    box = layout.box()
    row = box.row()
    row.prop(sp, prop_name,
             text=iface_(label, _I18N_CTX),
             icon='TRIA_DOWN' if getattr(sp, prop_name) else 'TRIA_RIGHT',
             emboss=False)
    return box


# Apply-button label per method — the button always applies whatever the dropdown shows.
_APPLY_LABEL = {
    'TRIPLANAR': ("Apply Mapping",   'FILE_REFRESH'),
    'OBJECT':    ("Apply Mapping",   'FILE_REFRESH'),
    'KEEP':      ("Use Existing UV", 'UV_DATA'),
    'UV':        ("Unwrap & Apply",  'MOD_UVPROJECT'),
    'CUBE':      ("Unwrap & Apply",  'MOD_UVPROJECT'),
    'SLIM':      ("Unwrap & Apply",  'MOD_UVPROJECT'),
}


def _draw_uv_finish(layout, obj, sp, idx, mat):
    """Mapping dropdown → status line → (shortcut) → apply.

    One click gives a triplanar preview: it creates no UV map and ignores any the mesh has.
    That stays invisible until an export fails, so the status is always on screen. The status
    is read from the built material, not from sp.uv_method — a shared or older material can
    disagree with the scene setting. The apply button only rewires the material, so
    hand-added nodes survive; Rebuild PBR Nodes clears the whole graph, so it is demoted to
    the icon button beside it.
    """
    layout.prop(sp, "uv_method", text="", icon='UV_DATA')

    status = fn_uv_status(obj, sp.uv_method, mat)
    text, icon = _UV_STATUS[status]
    line = layout.row()
    line.alert = status in ('NO_UV', 'BROKEN')
    line.label(text=text, icon=icon)

    # Shortcut for the one move that makes sense from here — saves opening the dropdown.
    if status in ('NO_UV', 'BROKEN'):
        layout.operator("kilnkit.finish_uv", text="Create UV Map", icon='MOD_UVPROJECT').mode = 'UNWRAP'
    elif status == 'IGNORED':
        layout.operator("kilnkit.finish_uv", text="Use Existing UV", icon='UV_DATA').mode = 'KEEP'

    label, licon = _APPLY_LABEL.get(sp.uv_method, ("Apply Mapping", 'FILE_REFRESH'))
    row = layout.row(align=True)
    apply_row = row.row(align=True)
    apply_row.enabled = not (sp.uv_method == 'KEEP' and not fn_mesh_has_uv(obj))
    apply_row.operator("kilnkit.reapply_uv", text=label, icon=licon).slot_index = idx
    # Rebuild clears the node graph (hand-added nodes are lost) — kept, but out of the way.
    row.operator("kilnkit.rebuild_pbr", text="", icon='FILE_REFRESH').slot_index = idx


def _slot_has_faces(obj, idx):
    """Whether the slot idx material is assigned to at least one face — for the
    multi-slot "not assigned" hint. numpy bulk read keeps high-poly meshes cheap
    (only called with two or more slots)."""
    polys = obj.data.polygons
    n = len(polys)
    if n == 0:
        return False
    try:
        import numpy as np
        arr = np.empty(n, dtype=np.int32)
        polys.foreach_get("material_index", arr)
        return bool((arr == idx).any())
    except Exception:
        return any(p.material_index == idx for p in polys)


# ================================================================
# Main panel
# ================================================================

class KILNKIT_PT_Panel(bpy.types.Panel):
    bl_label       = f"Kilnkit {ADDON_VERSION}"
    bl_idname      = "KILNKIT_PT_panel"
    bl_space_type  = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category    = "Kilnkit"

    def draw(self, context):
        l   = self.layout
        sp  = context.scene.kilnkit_scene_props
        obj = context.active_object

        _draw_tab_bar(l, sp)

        # ── Main tab ─────────────────────────────────────────
        if sp.active_tab == 'MAIN':
            self._draw_main(context, l, sp, obj)

        # ── Settings tab ─────────────────────────────────────
        elif sp.active_tab == 'SETTINGS':
            self._draw_settings(context, l, sp, obj)

        # ── Batch tab ────────────────────────────────────────
        elif sp.active_tab == 'BATCH':
            self._draw_batch(context, l, sp, obj)

        # ── Library tab ──────────────────────────────────────
        elif sp.active_tab == 'LIBRARY':
            self._draw_library(context, l, sp)

        # ── Render tab ───────────────────────────────────────
        elif sp.active_tab == 'RENDER':
            self._draw_render(context, l, sp)

    # ── Main ─────────────────────────────────────────────────

    def _draw_main(self, context, l, sp, obj):
        if not obj or obj.type != 'MESH':
            l.label(text="Select a mesh object", icon='INFO')
            return

        # Object info + Decimate
        info = l.box()
        row  = info.row()
        row.label(text=obj.name, icon='OBJECT_DATA')
        row.label(text=iface_("Polygons  {n:,}").format(n=len(obj.data.polygons)))
        opt = info.row(align=True)
        opt.prop(sp, "show_mesh_opt", text="Decimate",
                 icon='TRIA_DOWN' if sp.show_mesh_opt else 'TRIA_RIGHT',
                 emboss=False)
        if sp.show_mesh_opt:
            col = info.column(align=True)
            col.prop(sp, "decimate_ratio", slider=True)
            col.operator("kilnkit.decimate", text="Apply", icon='MOD_DECIM')

        l.separator()
        slots = obj.kilnkit_slots

        # No slots yet — one folder pick sets up slot, UV, and PBR
        if not slots:
            box = l.box()
            # If the object already has materials (dragged in etc.), offer slot recognition first
            if any(m for m in obj.data.materials):
                box.label(text="This object already has materials", icon='INFO')
                r0 = box.row(); r0.scale_y = 1.5
                r0.operator("kilnkit.slots_from_object",
                            text="Slots from Materials", icon='IMPORT')
                box.separator()
            box.label(text="Picking a folder sets up slot, UV, and PBR automatically", icon='INFO')
            row = box.row()
            row.scale_y = 1.8
            row.operator("kilnkit.one_click", text="Start by Picking a Texture Folder",
                         icon='FILE_FOLDER').slot_index = 0
            box.separator()
            sub = box.row(align=True)
            sub.operator("kilnkit.add_slot", text="+ Empty Slot", icon='ADD')
            sub.operator("kilnkit.import_subfolders", text="Batch Subfolders", icon='NEWFOLDER')
            if any(m.get("kilnkit_lib") for m in bpy.data.materials):
                box.operator("kilnkit.slot_from_library", text="Import from Library", icon='ASSET_MANAGER')
            box.label(text="…or drop a texture file", icon='IMPORT')
            return

        # Slot list
        row_list = l.row()
        row_list.template_list(
            "KILNKIT_UL_slot_list", "",
            obj, "kilnkit_slots",
            sp, "active_slot_index",
            rows=3, maxrows=6
        )
        col_btn = row_list.column(align=True)
        col_btn.operator("kilnkit.add_slot",    text="", icon='ADD')
        col_btn.operator("kilnkit.remove_slot", text="", icon='REMOVE').slot_index = sp.active_slot_index
        col_btn.separator()
        col_btn.operator("kilnkit.import_subfolders", text="", icon='NEWFOLDER')
        col_btn.separator()
        col_btn.operator("kilnkit.move_slot", text="", icon='TRIA_UP').direction   = 'UP'
        col_btn.operator("kilnkit.move_slot", text="", icon='TRIA_DOWN').direction = 'DOWN'

        # Selected slot detail
        idx = sp.active_slot_index
        if 0 <= idx < len(slots):
            entry   = slots[idx]
            has_mat = idx < len(obj.data.materials) and obj.data.materials[idx]

            detail = l.box()

            # Base Color preview + folder/detection info side by side
            # With an image: thumbnail left + info right; without: info at full width
            bc_img = fn_get_basecolor_image(obj.data.materials[idx]) if has_mat else None
            if bc_img:
                top   = detail.split(factor=0.3)
                thumb = top.column()
                bc_img.preview_ensure()
                thumb.template_icon(icon_value=bc_img.preview.icon_id, scale=4)
                info  = top.column()
            else:
                info = detail

            # Folder path
            info.prop(entry, "directory", text="")

            _folder_changed = False
            # Texture detection (mtime-based cache — avoids os.listdir per redraw)
            if entry.directory:
                dir_abs = os.path.normpath(bpy.path.abspath(entry.directory))
                try:
                    mtime = os.path.getmtime(dir_abs)
                except OSError:
                    mtime = None
                cached = _tex_cache.get(dir_abs)
                if cached and cached[0] == mtime:
                    found = cached[1]
                    sidecar = cached[2] if len(cached) > 2 else None
                else:
                    found = fn_scan_textures(entry.directory)
                    sidecar = fn_find_sidecar(entry.directory)
                    _tex_cache[dir_abs] = (mtime, found, sidecar)

                _ALL_CHANNELS = ('basecolor', 'normal', 'roughness', 'height', 'ao', 'metallic')
                if found:
                    src = " (.mtlx)" if sidecar else ""   # show the source when a sidecar resolved it
                    info.label(
                        text=iface_("Detected{src}: {ch}").format(src=src, ch=_ch_join(found.keys())),
                        icon='CHECKMARK'
                    )
                    missing = [c for c in _ALL_CHANNELS if c not in found]
                    if missing:
                        info.label(
                            text=iface_("Missing: {ch}").format(ch=_ch_join(missing)),
                            icon='ERROR'
                        )
                    if has_mat and fn_slot_folder_synced(obj.data.materials[idx], entry.directory) is False:
                        _folder_changed = True
                else:
                    info.label(text="No textures found", icon='ERROR')
            else:
                info.label(text="Set a folder or drop a texture", icon='INFO')

            # Folder diverged from the material → offer reapply
            if _folder_changed:
                wcol = detail.column(align=True)
                wrow = wcol.row(); wrow.alert = True
                wrow.label(text="Folder changed — material still uses the old folder", icon='ERROR')
                wcol.operator("kilnkit.rebuild_pbr", text="Reapply from This Folder",
                              icon='FILE_REFRESH').slot_index = idx

            detail.separator()

            # Primary run button + Assign (enabled only with a material — assigns faces in multi-slot)
            row_main = detail.row(align=True)
            row_main.scale_y = 1.8
            row_main.operator("kilnkit.one_click", text="Apply PBR", icon='PLAY').slot_index = idx
            asg = row_main.row(align=True)
            asg.scale_x = 0.5
            asg.enabled = bool(has_mat)
            asg.operator("kilnkit.assign", text="Assign", icon='MATERIAL').slot_index = idx

            # Hint when the active slot's material is not yet on any face (multi-slot)
            if has_mat and len(slots) >= 2 and not _slot_has_faces(obj, idx):
                hint = detail.row()
                hint.alert = True
                hint.label(text="Slot not assigned — press [Assign] to apply to faces", icon='ERROR')

            # Mapping method — pick it here, apply it here (Settings keeps the per-method options)
            _active_mat = obj.data.materials[idx] if idx < len(obj.data.materials) else None
            _draw_uv_finish(detail, obj, sp, idx, _active_mat)

            # Mark/clear asset — puts the built material in the Asset Browser (material required)
            if has_mat:
                _is_asset = bool(obj.data.materials[idx].asset_data)
                detail.operator("kilnkit.toggle_asset",
                                text="Clear Asset" if _is_asset else "Mark as Asset",
                                icon='ASSET_MANAGER',
                                depress=_is_asset).slot_index = idx

            # Shared-material indicator + make single-user (shared material → sliders move together).
            # Exclude the fake user from the count — a library material (use_fake_user) on a single
            # mesh reports users==2, which would falsely read as "shared" across meshes.
            if has_mat:
                _sm = obj.data.materials[idx]
                _real_users = _sm.users - (1 if _sm.use_fake_user else 0)
                if _real_users > 1:
                    share = detail.row(align=True)
                    share.label(text=iface_("Shared ({n} users)").format(n=_real_users), icon='LINKED')
                    share.operator("kilnkit.make_single_user", text="Make Single-User",
                                   icon='DUPLICATE').slot_index = idx

            # Fine-tuning section
            detail.separator()
            row_adv = detail.row()
            row_adv.prop(sp, "show_advanced",
                         text="Fine Tuning",
                         icon='TRIA_DOWN' if sp.show_advanced else 'TRIA_RIGHT',
                         emboss=False)
            if sp.show_advanced:
                _adv_nodes = (obj.data.materials[idx].node_tree.nodes
                              if has_mat and obj.data.materials[idx].node_tree else None)
                if _adv_nodes and "KK_Mapping" in _adv_nodes:
                    sync_row = detail.row(align=True)
                    sync_row.prop(sp, "auto_sync", icon='UV_SYNC_SELECT')
                    sync_row.operator("kilnkit.sync_from_nodes", text="", icon='IMPORT')
                    col = detail.column(align=True)
                    # Texture scale — uniform slider by default, ▶ expands per-axis X/Y/Z
                    if entry.uv_scale_split:
                        hdr = col.row(align=True)
                        hdr.label(text="Texture Scale")
                        hdr.prop(entry, "uv_scale_split", text="", icon='TRIA_DOWN', emboss=False)
                        col.prop(entry, "uv_scale", text="", slider=True)
                    else:
                        hdr = col.row(align=True)
                        hdr.prop(entry, "uv_scale_uniform", text="Texture Scale", slider=True)
                        hdr.prop(entry, "uv_scale_split", text="", icon='TRIA_RIGHT', emboss=False)
                    # Edge blend only for triplanar (BOX projection) materials
                    if any(n.type == 'TEX_IMAGE' and n.projection == 'BOX' for n in _adv_nodes):
                        col.prop(entry, "triplanar_blend", slider=True)
                    # Only expose sliders for channels that actually exist in the node graph
                    if "KK_AO_Mix" in _adv_nodes:
                        col.prop(entry, "ao_strength", slider=True)
                    if "KK_NormalMap" in _adv_nodes:
                        col.prop(entry, "normal_strength", slider=True)
                    if "KK_Displacement" in _adv_nodes:
                        col.prop(entry, "height_scale", slider=True)
                else:
                    detail.label(text="Adjustable after applying PBR", icon='INFO')

    # ── Settings ─────────────────────────────────────────────

    def _draw_settings(self, context, l, sp, obj):

        # 1) One-click steps
        b1 = _section(l, sp, "Steps in One-Click", "show_pipeline")
        if sp.show_pipeline:
            col = b1.column(align=True)
            col.prop(sp, "step_scale")
            col.prop(sp, "step_uv", text=iface_("UV step ({m})").format(
                m=iface_(_MAP_METHOD_SHORT.get(sp.uv_method, sp.uv_method))))
            col.prop(sp, "step_pbr")

        # 2) UV settings
        b2 = _section(l, sp, "UV Settings", "show_uv")
        if sp.show_uv:
            b2.prop(sp, "uv_method", expand=True)
            # One-line purpose of the selected method (always visible, no hover needed)
            b2.label(text=_MAP_METHOD_DESC.get(sp.uv_method, ""), icon='INFO')
            if obj and obj.type == 'MESH' and obj.kilnkit_slots:
                idx2 = min(sp.active_slot_index, len(obj.kilnkit_slots) - 1)
                b2.operator("kilnkit.rebuild_pbr",
                             text="Rebuild PBR Nodes",
                             icon='FILE_REFRESH').slot_index = idx2
            b2.separator()
            if sp.uv_method in UV_OBJECT_COORD_METHODS:
                b2.label(text="No unwrap needed — coordinate-based mapping", icon='CHECKMARK')
            elif sp.uv_method == 'KEEP':
                b2.label(text="No unwrap — the mesh's own UV map is used", icon='CHECKMARK')
            else:
                if sp.uv_method in ('UV', 'SLIM'):
                    b2.prop(sp, "uv_angle_limit", slider=True)
                    b2.separator()
                b2.prop(sp, "use_pack_islands")
                if sp.use_pack_islands:
                    col2 = b2.column(align=True)
                    col2.prop(sp, "pack_rotate")
                    col2.prop(sp, "pack_margin", slider=True)

        # 3) Material settings
        b3 = _section(l, sp, "Material Settings", "show_material")
        if sp.show_material:
            b3.prop(sp, "mat_conflict")

        # 4) Filename rules (preset + custom)
        b4 = _section(l, sp, "Texture Filename Rules", "show_suffix")
        if sp.show_suffix:
            prefs = context.preferences.addons.get(__package__)
            if prefs:
                p = prefs.preferences
                b4.prop(p, "suffix_preset", text="Preset")
                b4.separator()
                col4 = b4.column(align=True)
                col4.prop(p, "suffix_basecolor")
                col4.prop(p, "suffix_normal")
                col4.prop(p, "suffix_roughness")
                col4.prop(p, "suffix_height")
                col4.prop(p, "suffix_ao")
                col4.prop(p, "suffix_metallic")
                b4.separator()
                b4.operator("kilnkit.reset_suffix", text="Reset Custom Defaults", icon='LOOP_BACK')
            else:
                b4.label(text="Editable when installed as an add-on", icon='INFO')

        # 5) Run steps individually (advanced)
        b5 = _section(l, sp, "Run Steps Individually (Advanced)", "show_step")
        if sp.show_step:
            if obj and obj.type == 'MESH' and obj.kilnkit_slots:
                idx = min(sp.active_slot_index, len(obj.kilnkit_slots)-1)
                col5 = b5.column(align=True)
                no_unwrap = sp.uv_method not in UV_UNWRAP_METHODS
                col5.operator("kilnkit.step_scale", text="1. Apply Scale",              icon='OBJECT_ORIGIN').slot_index = idx
                row_step_uv = col5.row()
                row_step_uv.enabled = not no_unwrap
                if sp.uv_method == 'KEEP':
                    _step_uv_text = "2. No unwrap — existing UV map"
                elif no_unwrap:
                    _step_uv_text = "2. No unwrap — coordinate-based"
                else:
                    _step_uv_text = iface_("2. {m}").format(m=iface_(_MAP_METHOD_SHORT.get(sp.uv_method, sp.uv_method)))
                row_step_uv.operator("kilnkit.step_uv", text=_step_uv_text, icon='UV').slot_index = idx
                col5.operator("kilnkit.step_pbr",   text="3. Build PBR Nodes",              icon='NODE_MATERIAL').slot_index  = idx
            else:
                b5.label(text="Set up a slot in the Main tab first", icon='INFO')

        # 6) Utilities
        b6 = _section(l, sp, "Utilities", "show_utils")
        if sp.show_utils:
            row6 = b6.row(align=True)
            row6.operator("kilnkit.reset_settings", text="Reset to Defaults", icon='LOOP_BACK')
            row6.operator("kilnkit.cleanup_images", text="Clean Up Images", icon='TRASH')
            b6.operator("kilnkit.dedup_materials", text="Merge Duplicate Materials", icon='MATERIAL_DATA')

    # ── Batch ────────────────────────────────────────────────

    def _draw_batch(self, context, l, sp, obj):
        selected = [o for o in context.selected_objects if o.type == 'MESH']
        active   = context.active_object

        status = l.box()
        status.label(text=iface_("Selected meshes: {n}").format(n=len(selected)), icon='OUTLINER_OB_MESH')
        if active and active.type == 'MESH':
            status.label(text=iface_("Reference object: {name}").format(name=active.name), icon='OBJECT_DATA')
            slot_count = len(active.kilnkit_slots)
            status.label(
                text=iface_("Slots: {n}").format(n=slot_count) if slot_count
                else iface_("No slots — add them in the Main tab"),
                icon='CHECKMARK' if slot_count else 'ERROR'
            )
        else:
            status.label(text="Select a mesh object", icon='ERROR')

        l.separator()
        info = l.box()
        info.label(text="Uses the reference object's slot setup", icon='INFO')
        info.label(text="to apply PBR to all selected meshes")
        l.separator()

        can_run = (active and active.type == 'MESH'
                   and len(active.kilnkit_slots) > 0
                   and len(selected) > 0)
        row = l.row()
        row.scale_y = 1.5
        row.enabled = can_run
        row.operator("kilnkit.batch_run", text="▶ Run Batch", icon='FILE_REFRESH')
        if not can_run:
            l.label(text="Set up a slot in the Main tab first", icon='INFO')

        # ── Naming ──
        l.separator()
        naming = l.box()
        naming.label(text="Naming", icon='SORTALPHA')

        # Asset name (base) — empty falls back to the folder name
        naming.prop(sp, "naming_base", text="Name")

        # Prefix
        row_pfx = naming.row(align=True)
        row_pfx.prop(sp, "naming_use_prefix", text="Prefix")
        if sp.naming_use_prefix:
            row_pfx.prop(sp, "naming_prefix", text="")

        # Rename-materials toggle
        naming.prop(sp, "naming_rename_mats")

        # Preview — object/material family from the base (user input or folder name)
        if active and active.type == 'MESH':
            base = fn_naming_base(sp, active)
            if base:
                pv = naming.box()
                pv.label(text=f"OBJ  {fn_naming_final(sp, base)}", icon='OBJECT_DATA')
                if sp.naming_rename_mats:
                    mats  = [m for m in active.data.materials if m]
                    multi = len(mats) > 1
                    for i, m in enumerate(active.data.materials):
                        if not m:
                            continue
                        nm = fn_naming_material_name(sp, base, fn_slot_folder_name(active, i), multi)
                        pv.label(text=f"MAT  {nm}", icon='MATERIAL')
                if not sp.naming_base.strip():
                    _si, _b = fn_first_slot_dir(active)
                    if _si is not None:
                        pv.label(text=iface_("Base: Slot {n} folder").format(n=_si+1), icon='DOT')

        # Buttons
        row_btn = naming.row(align=True)
        row_btn.operator("kilnkit.apply_naming",         text="Apply Naming", icon='CHECKMARK')
        row_btn.operator("kilnkit.remove_number_suffix", text="Remove .001", icon='X')

        # ── LOD generation ──
        l.separator()
        lod = l.box()
        lod.label(text="Generate LODs", icon='MOD_DECIM')
        rowl = lod.row(align=True)
        rowl.prop(sp, "lod_count")
        rowl.prop(sp, "lod_step")
        lod.operator("kilnkit.generate_lod", text="Generate LODs for Selected", icon='MOD_DECIM')

    # ── Library ──────────────────────────────────────────────

    def _draw_library(self, context, l, sp):
        l.label(text="Folder → material, no mesh needed", icon='ASSET_MANAGER')

        # Builder
        box = l.box()
        box.prop(sp, "library_dir", text="")
        if sp.library_dir:
            dir_abs = os.path.normpath(bpy.path.abspath(sp.library_dir))
            try:
                mtime = os.path.getmtime(dir_abs)
            except OSError:
                mtime = None
            cached = _tex_cache.get(dir_abs)
            if cached and cached[0] == mtime:
                found = cached[1]
                sidecar = cached[2] if len(cached) > 2 else None
            else:
                found = fn_scan_textures(sp.library_dir)
                sidecar = fn_find_sidecar(sp.library_dir)
                _tex_cache[dir_abs] = (mtime, found, sidecar)
            if found:
                src = " (.mtlx)" if sidecar else ""
                box.label(text=iface_("Detected{src}: {ch}").format(src=src, ch=_ch_join(found.keys())), icon='CHECKMARK')
            else:
                box.label(text="No textures found", icon='ERROR')
        else:
            box.label(text="Set a folder or choose with the buttons below", icon='INFO')
        row = box.row(align=True)
        row.scale_y = 1.3
        row.operator("kilnkit.lib_build",            text="Build Material",   icon='ADD')
        row.operator("kilnkit.lib_build_subfolders", text="Batch Subfolders", icon='NEWFOLDER')

        l.separator()

        libs = [m for m in bpy.data.materials if m.get("kilnkit_lib")]

        # Export — .blend files into the asset library folder (Kilnkit/)
        exp = l.row()
        exp.scale_y = 1.2
        exp.enabled = bool(libs)
        exp.operator("kilnkit.lib_export", text="Export to Library (.blend)", icon='EXPORT')

        # Open Asset Browser — browse exported materials and drag them onto meshes (published shelf)
        l.operator("kilnkit.open_asset_browser", text="Open Asset Browser", icon='WINDOW')

        l.separator()

        # Library material list — scrollable UIList (icon + name) + selected-item detail
        if not libs:
            l.label(text="No library materials yet", icon='INFO')
            return
        l.label(text=iface_("{n} library materials").format(n=len(libs)), icon='MATERIAL')
        l.template_list("KILNKIT_UL_lib_list", "", bpy.data, "materials",
                        sp, "library_active_index", rows=4, maxrows=10)

        idx = sp.library_active_index
        mat = bpy.data.materials[idx] if 0 <= idx < len(bpy.data.materials) else None
        if not (mat and mat.get("kilnkit_lib")):
            l.label(text="Select a material from the list", icon='INFO')
            return

        l.separator()
        l.label(text="Selected material ↓", icon='RESTRICT_SELECT_OFF')
        det = l.box()
        iid = _mat_icon_id(mat)
        if iid:
            top = det.split(factor=0.3)
            top.template_icon(icon_value=iid, scale=4)
            info = top.column()
        else:
            info = det
        info.label(text=mat.name, icon='MATERIAL')
        src = mat.get("kilnkit_src", "")
        if src:
            folder = os.path.basename(os.path.normpath(bpy.path.abspath(src)))
            info.label(text=folder or "Folder", icon='FILE_FOLDER')
            if fn_slot_folder_synced(mat, src) is False:
                info.label(text="Folder changed — rewire needed", icon='ERROR')
        btn = det.row(align=True)
        asg = btn.row(align=True)
        asg.enabled = bool(context.active_object and context.active_object.type == 'MESH')
        asg.operator("kilnkit.lib_assign", text="Apply to Mesh", icon='CHECKMARK').mat_name = mat.name
        btn.operator("kilnkit.lib_rebuild", text="", icon='FILE_REFRESH').mat_name = mat.name
        btn.operator("kilnkit.lib_remove",  text="", icon='TRASH').mat_name = mat.name

    # ── Render ───────────────────────────────────────────────

    def _draw_render(self, context, l, sp):
        l.label(text="Turn finished assets into presentation renders", icon='RENDER_STILL')

        # 1) Environment / background
        b1 = _section(l, sp, "Environment / Background", "show_render_env")
        if sp.show_render_env:
            b1.prop(sp, "render_light_preset", expand=True)
            if sp.render_light_preset == 'HDRI':
                b1.prop(sp, "render_hdri_path", text="HDRI")
            elif sp.render_light_preset == 'FLAT':
                b1.prop(sp, "world_color")
            b1.prop(sp, "world_strength", slider=True)
            b1.operator("kilnkit.setup_environment", text="Apply Environment", icon='WORLD')
            b1.separator()
            # Transparent background — expose the built-in property directly (one source of truth, no extra state)
            b1.prop(context.scene.render, "film_transparent", text="Transparent Background (PNG alpha)")

        # 2) Lighting
        b2 = _section(l, sp, "Lighting", "show_render_light")
        if sp.show_render_light:
            b2.label(text="Key / fill / rim lights, sized to your asset", icon='LIGHT')
            b2.operator("kilnkit.setup_studio_lights", text="Set Up 3-Point Lights", icon='LIGHT_AREA')
            if bpy.data.collections.get("KK_Render_Lights"):
                b2.label(text="Installed: KK_Render_Lights", icon='CHECKMARK')

        # 3) Camera
        b3 = _section(l, sp, "Camera", "show_render_camera")
        if sp.show_render_camera:
            sel = [o for o in context.selected_objects if o.type == 'MESH']
            b3.label(text="Angle — places one camera at this angle", icon='CAMERA_DATA')
            b3.prop(sp, "camera_view", expand=True)
            row = b3.row(align=True)
            row.prop(sp, "camera_lens")
            row.prop(sp, "camera_margin")
            r2 = b3.row()
            r2.scale_y = 1.2
            r2.enabled = bool(sel)
            r2.operator("kilnkit.setup_camera", text="Place Camera at This Angle", icon='CAMERA_DATA')
            if not sel:
                b3.label(text="Select a mesh to enable", icon='INFO')
            elif bpy.data.objects.get("KK_Camera"):
                b3.label(text="Installed: KK_Camera (scene camera)", icon='CHECKMARK')
                b3.label(text="Freely adjust the camera — Render uses it as-is", icon='INFO')
            b3.label(text="All 4 angles at once → 'Render 4 Multi-Angles' below", icon='INFO')

        # 4) Render settings / output
        b4 = _section(l, sp, "Render Settings & Output", "show_render_output")
        if sp.show_render_output:
            b4.prop(sp, "render_engine_choice", expand=True)
            row = b4.row(align=True)
            row.prop(sp, "render_res", text="")
            row.prop(sp, "render_samples")
            b4.operator("kilnkit.apply_render_settings", text="Apply Render Settings", icon='PREFERENCES')
            b4.separator()
            # Native Blender output field, embedded directly (no separate "Save To") so Kilnkit
            # and F12 write to the same place. Kilnkit uses only the folder + its own file name.
            b4.prop(context.scene.render, "filepath", text="Output")
            b4.label(text="Folder only — file name = asset name_angle", icon='INFO')
            # Unsaved + unresolvable (//) or default (/tmp) path → renders fall back to Home.
            _fp = context.scene.render.filepath or "//"
            if not bpy.data.filepath and (_fp.startswith("//") or _fp.replace("\\", "/").rstrip("/") == "/tmp"):
                b4.label(text="Not saved — using Home/Kilnkit_Renders", icon='INFO')
            b4.prop(sp, "render_exist_mode")
            r = b4.row()
            r.scale_y = 1.4
            r.enabled = bool(context.scene.camera)
            r.operator("kilnkit.render_save", text="Render & Save", icon='RENDER_STILL')
            if not context.scene.camera:
                b4.label(text="Place the camera first", icon='INFO')
            # 4 multi-angles — places its own camera per angle (needs selected meshes)
            selm = [o for o in context.selected_objects if o.type == 'MESH']
            r3 = b4.row()
            r3.scale_y = 1.2
            r3.enabled = bool(selm)
            r3.operator("kilnkit.render_multi_angle", text="4 Multi-Angles (Front · 3/4 · Side · Top)", icon='CAMERA_DATA')

            # Turntable — 360° camera orbit animation (places its own camera; needs selected meshes)
            b4.separator()
            b4.label(text="Turntable — a 360° spin video", icon='FILE_MOVIE')
            trow = b4.row(align=True)
            trow.prop(sp, "turntable_frames")
            trow.prop(sp, "turntable_fps")
            b4.prop(sp, "turntable_format", text="")
            # Read-only: probing writes to the scene, which draw() is not allowed to do.
            if sp.turntable_format == 'MP4' and fn_ffmpeg_known_missing():
                b4.label(text="No FFmpeg in this Blender — saves as PNG sequence", icon='INFO')
            elif sp.turntable_format == 'MP4' and context.scene.render.film_transparent:
                # Video has no alpha channel → transparent areas render black.
                b4.label(text="Transparent bg → MP4 shows black", icon='INFO')
                b4.label(text="(PNG sequence keeps the alpha)")
            r4 = b4.row()
            r4.scale_y = 1.2
            r4.enabled = bool(selm)
            r4.operator("kilnkit.render_turntable", text="Render Turntable", icon='FILE_MOVIE')

        # 5) Render queue (batch, paid module) — draws its own collapsible box.
        #    The lite build omits the module and shows nothing here: Blender's
        #    Extensions policy forbids advertising the paid version inside the
        #    add-on UI, so the upsell lives on the store/listing page (a
        #    description link), never in the panel.
        if render_queue is not None and EDITION != 'lite':
            render_queue.draw_queue(context, l)


# ================================================================
# Registration
# ================================================================

classes = (
    KILNKIT_UL_SlotList,
    KILNKIT_UL_LibList,
    KILNKIT_PT_Panel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
