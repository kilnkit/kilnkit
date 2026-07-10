import bpy
from . import props, operators, ui, translations
try:
    from . import render_queue          # paid module — absent in the lite build
except Exception:
    render_queue = None

bl_info = {
    "name":        "Kilnkit",
    "author":      "Deokho Kim",
    "version":     (1, 0, 0),
    "blender":     (4, 5, 0),
    "location":    "View3D > Sidebar > Kilnkit",
    "description": "Point at a texture folder and get UV, PBR nodes, and naming automatically",
    "category":    "Material",
}


def register():
    props.register()
    operators.register()
    if render_queue is not None:
        render_queue.register()
    ui.register()
    bpy.app.translations.register(__name__, translations.translations_dict)


def unregister():
    bpy.app.translations.unregister(__name__)
    ui.unregister()
    if render_queue is not None:
        render_queue.unregister()
    operators.unregister()
    props.unregister()
