# vim: tabstop=8 expandtab shiftwidth=4 softtabstop=4

#  ***** BEGIN GPL LICENSE BLOCK *****
#
#  This program is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
#  The Original Code is Copyright (C) 2012 by Philip Lafleur.
#  All rights reserved.
#
#  Contact:      bksnzq {at} gmail {dot} com
#
#  The Original Code is: all of this file.
#
#  Contributor(s): none yet.
#
#  ***** END GPL LICENSE BLOCK *****

bl_info = {
    "name": "Restore Symmetry (originally Remirror)",
    "author": "Philip Lafleur (original author), Henrik Berglund (edits), Sergey Meshkov (edits)",
    "version": (1, 0, 3),
    "blender": (2, 80, 0),
    "location": "View3D > Object > Mirror > Restore Symmetry",
    "description": "Non-destructively update symmetry of a mirrored mesh (and shapekeys)",
    "warning": "",
    "wiki_url": ("Original wiki: http://wiki.blender.org/index.php/Extensions:2.6"
                 "/Py/Scripts/Mesh/Remirror"),
    "tracker_url": ("Original tracker post (now archived): http://projects.blender.org/tracker/index.php?"
                    "func=detail&aid=32166&group_id=153&atid=467"),
    "category": "Mesh"}

import bpy
import bmesh

ERR_ASYMMETRY    = "Asymmetry encountered (central edge loop(s) not centered?)"
ERR_BAD_PATH     = "Couldn't follow edge path (inconsistent normals?)"
ERR_CENTRAL_LOOP = "Failed to find central edge loop(s). Please recenter."
ERR_FACE_COUNT   = "Encountered edge with more than 2 faces attached."

CENTRAL_LOOP_MARGIN = 1e-5


class RestoreSymmetry_OT_RestoreSymmetry(bpy.types.Operator):
    bl_idname      = "mesh.restoresymmetry"
    bl_label       = "Restore Symmetry"
    bl_description = "Non-destructively update symmetry of a mirrored mesh (and shapekeys)"
    bl_options     = {'REGISTER', 'UNDO'}

    axis: bpy.props.EnumProperty(
                 name = "Axis",
                 description = "Mirror axis",
                 default = 'X',
                 items = (('X', "X", "X Axis"),
                          ('Y', "Y", "Y Axis"),
                          ('Z', "Z", "Z Axis")))
    source: bpy.props.EnumProperty(
                 name = "Source",
                 description = "Which half of the mesh to use as mirror source",
                 default = 'POSITIVE',
                 items = (('POSITIVE', "Positive side", "Positive side"),
                          ('NEGATIVE', "Negative side", "Negative side")))
    targetmix: bpy.props.FloatProperty(
                 name = "Target Mix Amount",
                 description = "How much target coordinates should contribute",
                 default = 0.0, min = 0.0, max = 1.0)

    @classmethod
    def poll (cls, context):
        obj = context.active_object
        return obj and obj.type == 'MESH' and context.mode == 'OBJECT' or 'EDIT_MESH' or 'SCULPT'

    def execute(self, context):
        mesh = bpy.context.active_object.data
        mode = bpy.context.mode

        if bpy.context.active_object.data.shape_keys is None:
            shapekey = None
        else:
            shapekey = bpy.context.active_object.active_shape_key.name #if shapekey was found add to shapekey for later

        bpy.ops.object.mode_set(mode='OBJECT') #go to object mode for bmesh operation

        try:
            restore_symmetry(mesh, shapekey, {'X': 0, 'Y': 1, 'Z': 2}[self.axis], self.source, self.targetmix)
        except ValueError as e:
            self.report ({'ERROR'}, str(e))

        if mode == 'EDIT_MESH':
            bpy.ops.object.mode_set(mode='EDIT') #return to edit mode
        if mode == 'SCULPT':
            bpy.ops.object.mode_set(mode='SCULPT')  # return to sculpt mode

        return {'FINISHED'}


def next_edgeCCW(v, e_prev):
    """
    Return the edge following e_prev in counter-clockwise order around vertex v
    by following the winding of the surrounding faces.
    """
    if len(e_prev.link_loops) == 2:
        # Assumes continuous normals
        if e_prev.link_loops[0].vert is v:
            return e_prev.link_loops[0].link_loop_prev.edge
        return e_prev.link_loops[1].link_loop_prev.edge

    elif len(e_prev.link_loops) == 1:
        # Assumes only two single-loop edges per vertex
        if e_prev.link_loops[0].vert is v:
            return e_prev.link_loops[0].link_loop_prev.edge
        for edge in v.link_edges:
            if len(edge.link_loops) == 1 and edge is not e_prev:
                return edge

    else:
        raise ValueError(ERR_FACE_COUNT)


def next_edge_CW(v, e_prev):
    """
    Return the edge following e_prev in clockwise order around vertex v
    by following the winding of the surrounding faces.
    """
    if len(e_prev.link_loops) == 2:
        # Assumes continuous normals
        if e_prev.link_loops[0].vert is not v:
            return e_prev.link_loops[0].link_loop_next.edge
        return e_prev.link_loops[1].link_loop_next.edge

    elif len(e_prev.link_loops) == 1:
        # Assumes only two single-loop edges per vertex
        if e_prev.link_loops[0].vert is not v:
            return e_prev.link_loops[0].link_loop_next.edge
        for edge in v.link_edges:
            if len (edge.link_loops) == 1 and edge is not e_prev:
                return edge

    else:
        raise ValueError (ERR_FACE_COUNT)


def visit_mirror_verts(v_start, e_start, visitor, shapelayer, shapekey):
    """
    Call visitor(v_right, v_left) for each pair of mirrored vertices that
    are reachable by following a path from v_start along connected edges
    without intersecting the central edge loop(s) or any previously-visited
    vertices.

    v_start: a vertex on a central edge loop
    e_start: an edge on a central edge loop such that the next edge in
             counter-clockwise order around v_start is on the positive side
             of the central loop
    """
    er = e_start
    el = e_start
    vr = v_start
    vl = v_start
    path = [(er, el)]

    while path:
        er = next_edgeCCW(vr, er)
        el = next_edge_CW(vl, el)

        if er is path[-1][0] or er.tag:
            if not (el is path[-1][1] or el.tag):
                raise ValueError(ERR_ASYMMETRY)
            er = path[-1][0]
            el = path[-1][1]
            vr = er.other_vert(vr)
            vl = el.other_vert(vl)
            path.pop()
            continue

        if el is path[-1][1] or el.tag:
            raise ValueError(ERR_ASYMMETRY)

        vr = er.other_vert(vr)
        vl = el.other_vert(vl)

        if vr is None:
            raise ValueError(ERR_BAD_PATH)
        if vr.tag:
            if vl is None or not vl.tag:
                raise ValueError(ERR_ASYMMETRY)
            vr = er.other_vert(vr)
            vl = el.other_vert(vl)
            continue

        if vl is None or vl.tag:
            raise ValueError(ERR_ASYMMETRY)

        path.append((er, el))
        visitor(vr, vl)
        vr.tag = True
        vl.tag = True


def update_verts(v_start, e_start, axis, source, shapelayer, shapekey, targetmix):
    def update_positive(v_right, v_left):
        if(shapekey=="Basis" or shapekey == None): #no shapekeys or basis shapekey selected - use original code
        # mix source and target (default mix amount 0 means use 100% source); mix at target, then update source 
            v_left.co = targetmix*v_left.co + (1.0-targetmix)*v_right.co
            v_left.co[axis] = v_left.co[axis] - 2.0*(1.0-targetmix)*v_right.co[axis]
            v_right.co = v_left.co
            v_right.co[axis] = -v_left.co[axis]
        else: #shapekeys found - use edited code
        # mix source and target (default mix amount 0 means use 100% source); mix at target, then update source
            v_left[shapelayer] = targetmix*v_left[shapelayer] + (1.0-targetmix)*v_right[shapelayer]
            v_left[shapelayer][axis] = v_left[shapelayer][axis] - 2.0*(1.0-targetmix)*v_right[shapelayer][axis]
            v_right[shapelayer] = v_left[shapelayer]
            v_right[shapelayer][axis] = -v_left[shapelayer][axis]

    def update_negative(v_right, v_left):
        if(shapekey=="Basis" or shapekey == None): #no shapekeys or basis shapekey selected - use original code
        # mix source and target (default mix amount 0 means use 100% source); mix at target, then update source 
            v_right.co = targetmix*v_right.co + (1.0-targetmix)*v_left.co
            v_right.co[axis] = v_right.co[axis] - 2.0*(1.0-targetmix)*v_left.co[axis]            
            v_left.co = v_right.co
            v_left.co[axis] = -v_right.co[axis]
        else: #shapekeys found - use edited code
        # mix source and target (default mix amount 0 means use 100% source); mix at target, then update source 
            v_right[shapelayer] = targetmix*v_right[shapelayer] + (1.0-targetmix)*v_left[shapelayer]
            v_right[shapelayer][axis] = v_right[shapelayer][axis] - 2.0*(1.0-targetmix)*v_left[shapelayer][axis]
            v_left[shapelayer] = v_right[shapelayer]
            v_left[shapelayer][axis] = -v_right[shapelayer][axis]

    visit_mirror_verts(
        v_start, e_start,
        update_positive if source == 'POSITIVE' else update_negative, shapelayer, shapekey)


def tag_central_edge_path(v, e):
    """
    Tag each edge along the path starting at edge e in the direction of vertex v
    such that the path evenly divides the number of edges connected to each
    vertex.
    """
    while True:
        e.tag = True

        if len(v.link_edges) % 2:
            if len(v.link_faces) == len(v.link_edges):
                raise ValueError(ERR_CENTRAL_LOOP)
            else:
                return

        for i in range(len(v.link_edges) // 2):
            e = next_edgeCCW(v, e)

        v = e.other_vert(v)
        if v is None:
            raise ValueError(ERR_BAD_PATH)

        if e.tag:
            return


def tag_central_loops (bm, axis):
    """
    Attempt to find and tag the edges on the central loop(s) of the bmesh bm
    aligned with the given axis.
    """
    for v in bm.verts:
        v.tag = False
    for e in bm.edges:
        e.tag = False

    verts = []
    edges = []

    for v in bm.verts:
        if CENTRAL_LOOP_MARGIN > v.co[axis] > -CENTRAL_LOOP_MARGIN:
            v.tag = True
            verts.append(v)

    for v in verts:
        for e in v.link_edges:
            if e.other_vert(v).tag:
                e.tag = True
                edges.append(e)

    for v in verts:
        v.tag = False

    if not(edges and verts):
        raise ValueError(ERR_CENTRAL_LOOP)

    for e in edges:
        tag_central_edge_path(e.verts[0], e)
        tag_central_edge_path(e.verts[1], e)


def starting_vertex(edge, axis):
    """
    Return the endpoint of the given edge such that the next edge in
    counter-clockwise order around the endpoint is on the positive side of
    the given axis.
    """
    if len(edge.link_loops) != 2:
        raise ValueError(ERR_FACE_COUNT)

    loops = sorted(edge.link_loops,
                    key = lambda loop: loop.face.calc_center_median()[axis])

    return loops[-1].vert


def restore_symmetry(mesh, shapekey, axis, source, targetmix):
    bm = bmesh.new ()
    bm.from_mesh(mesh)

    if shapekey is None:
        shapelayer = None
    else:
        shapelayer = bm.verts.layers.shape[shapekey] #if the mesh had shapekeys, set the BM layer for the shapekey

    tag_central_loops(bm, axis)

    for e in bm.edges:
        if e.tag:
            e.verts[0].co[axis] = 0.
            e.verts[1].co[axis] = 0.
            e.verts[0].tag = True
            e.verts[1].tag = True

    for e in bm.edges:
        if e.tag:
            update_verts(starting_vertex(e, axis), e, axis, source, shapelayer, shapekey, targetmix)

    for v in bm.verts:
        v.tag = False
    for e in bm.edges:
        e.tag = False

    bm.to_mesh(mesh)
    mesh.update()


def menufunc(self, context):
    self.layout.operator(RestoreSymmetry_OT_RestoreSymmetry.bl_idname)


def register():
    bpy.utils.register_class(RestoreSymmetry_OT_RestoreSymmetry)
    bpy.types.VIEW3D_MT_mirror.append(menufunc)


def unregister():
    bpy.types.VIEW3D_MT_mirror.remove(menufunc)
    bpy.utils.unregister_class(RestoreSymmetry_OT_RestoreSymmetry)


if __name__ == "__main__":
    register()
