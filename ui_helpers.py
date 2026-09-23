"""Shared, read-only layout primitives for task panels in either edition."""
import bpy
import blf
from bpy.app.translations import pgettext_iface as iface_


def panel_width(context=None):
    """Width in UI coordinates, including sidebar zoom and interface scaling."""
    context = context or bpy.context
    region = context.region
    if region is None:
        return 280
    try:
        left = region.view2d.region_to_view(0, 0)[0]
        right = region.view2d.region_to_view(region.width, 0)[0]
        return abs(right - left)
    except Exception:
        return region.width / max(context.preferences.system.ui_scale, 0.5)


def wrapped_lines(text, width, measure):
    """Wrap translated text, including CJK and long unbroken filenames."""
    lines = []
    for paragraph in text.splitlines() or ['']:
        rest = paragraph.strip()
        if not rest:
            lines.append('')
        while rest:
            end = 1
            while end < len(rest) and measure(rest[:end + 1]) <= width:
                end += 1
            if end < len(rest):
                space = rest.rfind(' ', 0, end + 1)
                if space > end // 2:
                    end = space
            lines.append(rest[:end].rstrip())
            rest = rest[end:].lstrip()
    return lines


def note(layout, text, icon='NONE', *, translate=True, inset=0):
    """A wrapping label. Reserve room for box padding, scroll bar, and icon."""
    text = iface_(text) if translate else text
    blf.size(0, 11)
    # BLF and widget glyph metrics differ slightly. Keep a conservative inset so
    # unbroken paths also fit with the sidebar tabs and nested-box padding.
    width = max(60, panel_width() - 88 - inset - (20 if icon != 'NONE' else 0))
    col = layout.column(align=True)
    col.scale_y = 0.95
    for i, line in enumerate(wrapped_lines(text, width, lambda s: blf.dimensions(0, s)[0])):
        col.label(text=line, icon=icon if i == 0 else ('BLANK1' if icon != 'NONE' else 'NONE'),
                  translate=False)


def section(layout, data, label, prop):
    box = layout.box()
    box.prop(data, prop, text=iface_(label, 'Kilnkit'), translate=False,
             icon='TRIA_DOWN' if getattr(data, prop) else 'TRIA_RIGHT', emboss=False)
    return box


def output_destination(layout, context):
    """Native output settings, with the effective asset name visible before running."""
    from .operators import fn_naming_base_status, fn_render_outdir_display
    scene = context.scene
    base, mesh_name, overriding = fn_naming_base_status(context)
    note(layout, iface_("File name base: {base}").format(base=base), 'OBJECT_DATA', translate=False)
    if overriding:
        row = layout.column()
        row.alert = True
        note(row, iface_("Asset Name overrides — selected mesh is '{m}'").format(m=mesh_name),
             'ERROR', translate=False)
    layout.prop(scene.render, 'filepath', text='Folder')
    note(layout, fn_render_outdir_display(context), 'FILE_FOLDER', translate=False)
    r = scene.render
    rx, ry = int(r.resolution_x * r.resolution_percentage / 100), int(r.resolution_y * r.resolution_percentage / 100)
    layout.label(text=f"{rx} × {ry} PNG", icon='IMAGE_DATA', translate=False)


def output_result(layout, context):
    """Report a file found for THIS asset, not an unverified claim of completion."""
    from .operators import fn_guide_output_name, fn_render_outdir_display
    name = fn_guide_output_name(context)
    if not name:
        return
    box = layout.box()
    box.label(text='Found in Output Folder', icon='CHECKMARK')
    note(box, name, translate=False)
    box.operator('wm.path_open', text='Open Output Folder', icon='FILE_FOLDER').filepath = fn_render_outdir_display(context)
