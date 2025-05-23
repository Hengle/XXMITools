import collections
import json
import os
import struct
import time
from pathlib import Path
from typing import Optional, Callable

import bmesh
import bpy
import numpy
from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import Collection, Context, Mesh, Object, Operator, PropertyGroup
from bpy_extras.io_utils import ExportHelper

from .datahandling import (
    Fatal,
    custom_attributes_float,
    custom_attributes_int,
    keys_to_ints,
    mesh_triangulate,
)
from .datastructures import (
    GameEnum,
    HashableVertex,
    IndexBuffer,
    InputLayout,
    VertexBufferGroup,
    f16_pattern,
    f32_pattern,
    game_enum,
    s8_pattern,
    s16_pattern,
    s32_pattern,
    snorm8_pattern,
    snorm16_pattern,
    u8_pattern,
    u16_pattern,
    u32_pattern,
    unorm8_pattern,
    unorm16_pattern,
)
from .data.byte_buffer import (
    BufferLayout,
    Semantic,
    AbstractSemantic,
    MigotoFmt,
    NumpyBuffer,
)
from .data.dxgi_format import DXGIType
from .exporter import ModExporter


def normal_export_translation(
    layouts: list[BufferLayout], semantic: Semantic, flip: bool
) -> Callable:
    unorm = False
    for layout in layouts:
        try:
            unorm = layout.get_element(AbstractSemantic(semantic)).format.dxgi_type in [
                DXGIType.UNORM8,
                DXGIType.UNORM16,
            ]
        except ValueError:
            continue
    if unorm:
        # Scale normal range -1:+1 to UNORM range 0:+1
        if flip:
            return lambda x: -x / 2.0 + 0.5
        return lambda x: x / 2.0 + 0.5
    if flip:
        return lambda x: -x
    return lambda x: x


def unit_vector(vector):
    a = numpy.linalg.norm(vector, axis=max(len(vector.shape) - 1, 0), keepdims=True)
    return numpy.divide(vector, a, out=numpy.zeros_like(vector), where=a != 0)


def antiparallel_search(ConnectedFaceNormals) -> bool:
    a = numpy.einsum("ij,kj->ik", ConnectedFaceNormals, ConnectedFaceNormals)
    return numpy.any((a > -1.000001) & (a < -0.999999))


def precision(x) -> int:
    return -int(numpy.floor(numpy.log10(x)))


def recursive_connections(Over2_connected_points) -> bool:
    for entry, connectedpointentry in Over2_connected_points.items():
        if len(connectedpointentry & Over2_connected_points.keys()) < 2:
            Over2_connected_points.pop(entry)
            if len(Over2_connected_points) < 3:
                return False
            return recursive_connections(Over2_connected_points)
    return True


def checkEnclosedFacesVertex(
    ConnectedFaces, vg_set, Precalculated_Outline_data
) -> bool:
    Main_connected_points = {}
    # connected points non-same vertex
    for face in ConnectedFaces:
        non_vg_points = [p for p in face if p not in vg_set]
        if len(non_vg_points) > 1:
            for point in non_vg_points:
                Main_connected_points.setdefault(point, []).extend(
                    [x for x in non_vg_points if x != point]
                )
        # connected points same vertex
    New_Main_connect = {}
    for entry, value in Main_connected_points.items():
        for val in value:
            ivspv = Precalculated_Outline_data.get("Same_Vertex").get(val) - {val}
            intersect_sidevertex = ivspv & Main_connected_points.keys()
            if intersect_sidevertex:
                New_Main_connect.setdefault(entry, []).extend(
                    list(intersect_sidevertex)
                )
        # connected points same vertex reverse connection
    for key, value in New_Main_connect.items():
        Main_connected_points.get(key).extend(value)
        for val in value:
            Main_connected_points.get(val).append(key)
        # exclude for only 2 way paths
    Over2_connected_points = {
        k: set(v) for k, v in Main_connected_points.items() if len(v) > 1
    }

    return recursive_connections(Over2_connected_points)


def blender_vertex_to_3dmigoto_vertex_outline(
    mesh: Mesh,
    obj: Object,
    blender_loop_vertex,
    layout,
    texcoords,
    export_Outline: dict,
) -> dict:
    blender_vertex = mesh.vertices[blender_loop_vertex.vertex_index]
    pos = list(blender_vertex.undeformed_co)
    blp_normal = list(blender_loop_vertex.normal)
    vertex = {}
    seen_offsets = set()

    # TODO: Warn if vertex is in too many vertex groups for this layout,
    # ignoring groups with weight=0.0
    vertex_groups = sorted(blender_vertex.groups, key=lambda x: x.weight, reverse=True)

    for elem in layout:
        if elem.InputSlotClass != "per-vertex":
            continue

        if (elem.InputSlot, elem.AlignedByteOffset) in seen_offsets:
            continue
        seen_offsets.add((elem.InputSlot, elem.AlignedByteOffset))

        if elem.name == "POSITION":
            if "POSITION.w" in mesh.vertex_layers_float:
                vertex[elem.name] = pos + [
                    mesh.vertex_layers_float["POSITION.w"]
                    .data[blender_loop_vertex.vertex_index]
                    .value
                ]
            else:
                vertex[elem.name] = elem.pad(pos, 1.0)
        elif elem.name.startswith("COLOR"):
            if elem.name in mesh.vertex_colors:
                vertex[elem.name] = elem.clip(
                    list(
                        mesh.vertex_colors[elem.name]
                        .data[blender_loop_vertex.index]
                        .color
                    )
                )
            else:
                try:
                    vertex[elem.name] = list(
                        mesh.vertex_colors[elem.name + ".RGB"]
                        .data[blender_loop_vertex.index]
                        .color
                    )[:3] + [
                        mesh.vertex_colors[elem.name + ".A"]
                        .data[blender_loop_vertex.index]
                        .color[0]
                    ]
                except KeyError:
                    raise Fatal(
                        "ERROR: Unable to find COLOR property. Ensure the model you are exporting has a color attribute (of type Face Corner/Byte Color) called COLOR"
                    )
        elif elem.name == "NORMAL":
            vertex[elem.name] = elem.pad(blp_normal, 0.0)
        elif elem.name.startswith("TANGENT"):
            vertex[elem.name] = elem.pad(
                export_Outline.get(blender_loop_vertex.vertex_index, blp_normal),
                blender_loop_vertex.bitangent_sign,
            )
        elif elem.name.startswith("BINORMAL"):
            pass
        elif elem.name.startswith("BLENDINDICES"):
            i = elem.SemanticIndex * 4
            vertex[elem.name] = elem.pad([x.group for x in vertex_groups[i : i + 4]], 0)
        elif elem.name.startswith("BLENDWEIGHT"):
            # TODO: Warn if vertex is in too many vertex groups for this layout
            i = elem.SemanticIndex * 4
            vertex[elem.name] = elem.pad(
                [x.weight for x in vertex_groups[i : i + 4]], 0.0
            )
        elif elem.name.startswith("TEXCOORD") and elem.is_float():
            # FIXME: Handle texcoords of other dimensions
            uvs = []
            for uv_name in ("%s.xy" % elem.name, "%s.zw" % elem.name):
                if uv_name in texcoords:
                    uvs += list(texcoords[uv_name][blender_loop_vertex.index])

            vertex[elem.name] = uvs
        else:
            # Unhandled semantics are saved in vertex layers
            data = []
            for component in "xyzw":
                layer_name = "%s.%s" % (elem.name, component)
                if layer_name in mesh.vertex_layers_int:
                    data.append(
                        mesh.vertex_layers_int[layer_name]
                        .data[blender_loop_vertex.vertex_index]
                        .value
                    )
                elif layer_name in mesh.vertex_layers_float:
                    data.append(
                        mesh.vertex_layers_float[layer_name]
                        .data[blender_loop_vertex.vertex_index]
                        .value
                    )
            if data:
                vertex[elem.name] = data

        if elem.name not in vertex:
            print("NOTICE: Unhandled vertex element: %s" % elem.name)

    return vertex


def optimized_outline_generation(obj: Object, mesh: Mesh, outline_properties):
    """Outline optimization for genshin impact by HummyR#8131"""
    start_timer = time.time()
    (
        outline_optimization,
        toggle_rounding_outline,
        decimal_rounding_outline,
        angle_weighted,
        overlapping_faces,
        detect_edges,
        calculate_all_faces,
        nearest_edge_distance,
    ) = outline_properties
    export_outline = {}
    Precalculated_Outline_data = {}
    print(
        "\tOptimize Outline: " + obj.name.lower() + "; Initialize data sets         ",
        end="\r",
    )

    ################# PRE-DICTIONARY #####################

    verts_obj = mesh.vertices
    Pos_Same_Vertices = {}
    Pos_Close_Vertices = {}
    Face_Verts = {}
    Face_Normals = {}
    Numpy_Position = {}
    if detect_edges and toggle_rounding_outline:
        i_nedd = min(precision(nearest_edge_distance), decimal_rounding_outline) - 1
        i_nedd_increment = 10 ** (-i_nedd)

    searched_vertex_pos = set()
    for poly in mesh.polygons:
        i_poly = poly.index
        face_vertices = poly.vertices
        facenormal = numpy.array(poly.normal)
        Face_Verts.setdefault(i_poly, face_vertices)
        Face_Normals.setdefault(i_poly, facenormal)

        for vert in face_vertices:
            Precalculated_Outline_data.setdefault("Connected_Faces", {}).setdefault(
                vert, []
            ).append(i_poly)
            if vert in searched_vertex_pos:
                continue

            searched_vertex_pos.add(vert)
            vert_obj = verts_obj[vert]
            vert_position = vert_obj.undeformed_co

            if toggle_rounding_outline:
                Pos_Same_Vertices.setdefault(
                    tuple(
                        round(coord, decimal_rounding_outline)
                        for coord in vert_position
                    ),
                    {vert},
                ).add(vert)

                if detect_edges:
                    Pos_Close_Vertices.setdefault(
                        tuple(round(coord, i_nedd) for coord in vert_position), {vert}
                    ).add(vert)
            else:
                Pos_Same_Vertices.setdefault(tuple(vert_position), {vert}).add(vert)

            if angle_weighted:
                numpy_pos = numpy.array(vert_position)
                Numpy_Position.setdefault(vert, numpy_pos)

    for values in Pos_Same_Vertices.values():
        for vertex in values:
            Precalculated_Outline_data.setdefault("Same_Vertex", {}).setdefault(
                vertex, set(values)
            )

    if detect_edges and toggle_rounding_outline:
        print(
            "\tOptimize Outline: " + obj.name.lower() + "; Edge detection       ",
            end="\r",
        )
        Precalculated_Outline_data.setdefault("RepositionLocal", set())

        for vertex_group in Pos_Same_Vertices.values():
            FacesConnected = []
            for x in vertex_group:
                FacesConnected.extend(
                    Precalculated_Outline_data.get("Connected_Faces").get(x)
                )
            ConnectedFaces = [Face_Verts.get(x) for x in FacesConnected]

            if not checkEnclosedFacesVertex(
                ConnectedFaces, vertex_group, Precalculated_Outline_data
            ):
                for vertex in vertex_group:
                    break

                p1, p2, p3 = verts_obj[vertex].undeformed_co
                p1n = p1 + nearest_edge_distance
                p1nn = p1 - nearest_edge_distance
                p2n = p2 + nearest_edge_distance
                p2nn = p2 - nearest_edge_distance
                p3n = p3 + nearest_edge_distance
                p3nn = p3 - nearest_edge_distance

                coord = [
                    [round(p1n, i_nedd), round(p1nn, i_nedd)],
                    [round(p2n, i_nedd), round(p2nn, i_nedd)],
                    [round(p3n, i_nedd), round(p3nn, i_nedd)],
                ]

                for i in range(3):
                    z, n = coord[i]
                    zndifference = int((z - n) / i_nedd_increment)
                    if zndifference > 1:
                        for r in range(zndifference - 1):
                            coord[i].append(z - r * i_nedd_increment)

                closest_group = set()
                for pos1 in coord[0]:
                    for pos2 in coord[1]:
                        for pos3 in coord[2]:
                            try:
                                closest_group.update(
                                    Pos_Close_Vertices.get(tuple([pos1, pos2, pos3]))
                                )
                            except (KeyError, TypeError):
                                continue

                if len(closest_group) != 1:
                    for x in vertex_group:
                        Precalculated_Outline_data.get("RepositionLocal").add(x)

                    for v_closest_pos in closest_group:
                        if v_closest_pos not in vertex_group:
                            o1, o2, o3 = verts_obj[v_closest_pos].undeformed_co
                            if (
                                p1n >= o1 >= p1nn
                                and p2n >= o2 >= p2nn
                                and p3n >= o3 >= p3nn
                            ):
                                for x in vertex_group:
                                    Precalculated_Outline_data.get("Same_Vertex").get(
                                        x
                                    ).add(v_closest_pos)

    Connected_Faces_bySameVertex = {}
    for key, value in Precalculated_Outline_data.get("Same_Vertex").items():
        for vertex in value:
            Connected_Faces_bySameVertex.setdefault(key, set()).update(
                Precalculated_Outline_data.get("Connected_Faces").get(vertex)
            )

    ################# CALCULATIONS #####################

    RepositionLocal = Precalculated_Outline_data.get("RepositionLocal")
    IteratedValues = set()
    print(
        "\tOptimize Outline: " + obj.name.lower() + "; Calculation          ", end="\r"
    )

    for key, vertex_group in Precalculated_Outline_data.get("Same_Vertex").items():
        if key in IteratedValues:
            continue

        if not calculate_all_faces and len(vertex_group) == 1:
            continue

        FacesConnectedbySameVertex = list(Connected_Faces_bySameVertex.get(key))
        row = len(FacesConnectedbySameVertex)

        if overlapping_faces:
            ConnectedFaceNormals = numpy.empty(shape=(row, 3))
            for i_normal, x in enumerate(FacesConnectedbySameVertex):
                ConnectedFaceNormals[i_normal] = Face_Normals.get(x)
            if antiparallel_search(ConnectedFaceNormals):
                continue

        if angle_weighted:
            VectorMatrix0 = numpy.empty(shape=(row, 3))
            VectorMatrix1 = numpy.empty(shape=(row, 3))

        ConnectedWeightedNormal = numpy.empty(shape=(row, 3))
        i = 0
        for facei in FacesConnectedbySameVertex:
            vlist = Face_Verts.get(facei)

            vert0p = set(vlist) & vertex_group

            if angle_weighted:
                for vert0 in vert0p:
                    v0 = Numpy_Position.get(vert0)
                    vn = [Numpy_Position.get(x) for x in vlist if x != vert0]
                    VectorMatrix0[i] = vn[0] - v0
                    VectorMatrix1[i] = vn[1] - v0
            ConnectedWeightedNormal[i] = Face_Normals.get(facei)

            influence_restriction = len(vert0p)
            if influence_restriction > 1:
                numpy.multiply(
                    ConnectedWeightedNormal[i], 0.5 ** (1 - influence_restriction)
                )
            i += 1

        if angle_weighted:
            angle = numpy.arccos(
                numpy.clip(
                    numpy.einsum(
                        "ij, ij->i",
                        unit_vector(VectorMatrix0),
                        unit_vector(VectorMatrix1),
                    ),
                    -1.0,
                    1.0,
                )
            )
            ConnectedWeightedNormal *= angle[:, None]

        wSum = unit_vector(numpy.sum(ConnectedWeightedNormal, axis=0)).tolist()

        if wSum != [0, 0, 0]:
            if RepositionLocal and key in RepositionLocal:
                export_outline.setdefault(key, wSum)
                continue
            for vertexf in vertex_group:
                export_outline.setdefault(vertexf, wSum)
                IteratedValues.add(vertexf)
    print(
        f"\tOptimize Outline: {obj.name.lower()}; Completed in {time.time() - start_timer} seconds       "
    )
    return export_outline


def recursive_append_to(collection: Collection, destination: list[Object], depth=1):
    """Append all meshes in a collection to a list"""
    objs = [obj for obj in collection.objects if obj.type == "MESH"]
    sorted_objs = sorted(objs, key=lambda x: x.name)
    for obj in sorted_objs:
        destination.append((collection.name, depth, obj))
    for a_collection in collection.children:
        recursive_append_to(a_collection, destination, depth + 1)


def apply_modifiers_and_shapekeys(context: Context, obj: Object) -> Mesh:
    """Apply all modifiers to a mesh with shapekeys. Preserves shapekeys named Deform"""
    start_timer = time.time()
    deform_SKs = []
    total_applied = 0
    desgraph = context.evaluated_depsgraph_get()
    modifiers_to_apply = [mod for mod in obj.modifiers if mod.show_viewport]
    if obj.data.shape_keys is not None:
        deform_SKs = [
            sk.name
            for sk in obj.data.shape_keys.key_blocks
            if "deform" in sk.name.lower()
        ]
        total_applied = len(obj.data.shape_keys.key_blocks) - len(deform_SKs)

    if len(deform_SKs) == 0:
        mesh = obj.evaluated_get(desgraph).to_mesh()
    else:
        mesh = obj.to_mesh()
        result_obj = obj.copy()
        result_obj.data = mesh.copy()
        context.collection.objects.link(result_obj)
        for sk in obj.data.shape_keys.key_blocks:
            if sk.name not in deform_SKs:
                result_obj.shape_key_remove(sk)
        list_properties = []
        vert_count = -1
        bpy.ops.object.select_all(action="DESELECT")
        result_obj.select_set(True)
        bpy.ops.object.duplicate_move(
            OBJECT_OT_duplicate={"linked": False, "mode": "TRANSLATION"},
            TRANSFORM_OT_translate={
                "value": (0, 0, 0),
                "orient_type": "GLOBAL",
                "orient_matrix": ((1, 0, 0), (0, 1, 0), (0, 0, 1)),
                "orient_matrix_type": "GLOBAL",
                "constraint_axis": (False, False, False),
                "mirror": True,
                "use_proportional_edit": False,
                "proportional_edit_falloff": "SMOOTH",
                "proportional_size": 1,
                "use_proportional_connected": False,
                "use_proportional_projected": False,
                "snap": False,
                "snap_target": "CLOSEST",
                "snap_point": (0, 0, 0),
                "snap_align": False,
                "snap_normal": (0, 0, 0),
                "gpencil_strokes": False,
                "cursor_transform": False,
                "texture_space": False,
                "remove_on_cancel": False,
                "release_confirm": False,
                "use_accurate": False,
            },
        )
        copy_obj = context.view_layer.objects.active
        copy_obj.select_set(False)
        context.view_layer.objects.active = result_obj
        result_obj.select_set(True)
        # Store key shape properties
        for key_b in obj.data.shape_keys.key_blocks:
            properties_object = {}
            properties_object["name"] = key_b.name
            properties_object["mute"] = key_b.mute
            properties_object["interpolation"] = key_b.interpolation
            properties_object["relative_key"] = key_b.relative_key.name
            properties_object["slider_max"] = key_b.slider_max
            properties_object["slider_min"] = key_b.slider_min
            properties_object["value"] = key_b.value
            properties_object["vertex_group"] = key_b.vertex_group
            list_properties.append(properties_object)
            result_obj.shape_key_remove(key_b)
        # Set up Basis
        result_obj = result_obj.evaluated_get(desgraph)
        # bpy.ops.object.shape_key_add(from_mix=False)
        # for mod in modifiers_to_apply:
        #     bpy.ops.object.modifier_apply(modifier=mod.name)
        mesh = result_obj.to_mesh()
        vert_count = len(result_obj.data.vertices)
        result_obj.select_set(False)
        # Create a temp object to apply modifiers into once per SK
        for i in range(1, obj.data.shape_keys.key_blocks):
            context.view_layer.objects.active = copy_obj
            copy_obj.select_set(True)
            bpy.ops.object.duplicate_move(
                OBJECT_OT_duplicate={"linked": False, "mode": "TRANSLATION"},
                TRANSFORM_OT_translate={
                    "value": (0, 0, 0),
                    "orient_type": "GLOBAL",
                    "orient_matrix": ((1, 0, 0), (0, 1, 0), (0, 0, 1)),
                    "orient_matrix_type": "GLOBAL",
                    "constraint_axis": (False, False, False),
                    "mirror": True,
                    "use_proportional_edit": False,
                    "proportional_edit_falloff": "SMOOTH",
                    "proportional_size": 1,
                    "use_proportional_connected": False,
                    "use_proportional_projected": False,
                    "snap": False,
                    "snap_target": "CLOSEST",
                    "snap_point": (0, 0, 0),
                    "snap_align": False,
                    "snap_normal": (0, 0, 0),
                    "gpencil_strokes": False,
                    "cursor_transform": False,
                    "texture_space": False,
                    "remove_on_cancel": False,
                    "release_confirm": False,
                    "use_accurate": False,
                },
            )
            temp_obj = context.view_layer.objects.active
            for k in temp_obj.data.shape_keys.key_blocks:
                temp_obj.shape_key_remove(k)

            copy_obj.select_set(True)
            copy_obj.active_shape_key_index = i

            bpy.ops.object.shape_key_transfer(use_clamp=True)
            context.object.active_shape_key_index = 0
            bpy.ops.object.shape_key_remove()
            bpy.ops.object.shape_key_remove(all=True)
            for mod in modifiers_to_apply:
                bpy.ops.object.modifier_apply(modifier=mod.name)
            if vert_count != len(temp_obj.data.vertices):
                raise Fatal(
                    f"After modifier application, object {obj.name} has a different vertex count in shape key {i} than in the basis shape key. Manual resolution required."
                )
            copy_obj.select_set(False)
            context.view_layer.objects.active = result_obj
            result_obj.select_set(True)
            bpy.ops.object.join_shapes()
            result_obj.select_set(False)
            context.view_layer.objects.active = temp_obj
            bpy.ops.object.delete(use_global=False)
        # Restore shape key properties like name, mute etc.
        context.view_layer.objects.active = result_obj
        for i in range(0, obj.data.shape_keys.key_blocks):
            key_b = context.view_layer.objects.active.data.shape_keys.key_blocks[i]
            key_b.name = list_properties[i]["name"]
            key_b.interpolation = list_properties[i]["interpolation"]
            key_b.mute = list_properties[i]["mute"]
            key_b.slider_max = list_properties[i]["slider_max"]
            key_b.slider_min = list_properties[i]["slider_min"]
            key_b.value = list_properties[i]["value"]
            key_b.vertex_group = list_properties[i]["vertex_group"]
            rel_key = list_properties[i]["relative_key"]

            for j in range(0, obj.data.shape_keys.key_blocks):
                key_brel = context.view_layer.objects.active.data.shape_keys.key_blocks[
                    j
                ]
                if rel_key == key_brel.name:
                    key_b.relative_key = key_brel
                    break
            context.view_layer.objects.active.data.update()
        result_obj.select_set(False)
        context.view_layer.objects.active = copy_obj
        copy_obj.select_set(True)
        bpy.ops.object.delete(use_global=False)
        bpy.ops.object.select_all(action="DESELECT")
        context.view_layer.objects.active = result_obj
        context.view_layer.objects.active.select_set(True)
        mesh = result_obj.data
        bpy.ops.object.delete(use_global=False)

    print(
        f"\tApplied {len(modifiers_to_apply)} modifiers, {total_applied} shapekeys and stored {len(deform_SKs)} shapekeys in {time.time() - start_timer:.5f} seconds"
    )
    return mesh


def create_mod_folder(destination: Path) -> None:
    if not destination.exists():
        print(f"Creating {destination.name} folder")
        os.mkdir(destination)
    else:
        print(
            f"WARNING: Everything currently in the {destination.name} folder will be overwritten"
        )


def collect_vb(folder: Path, name: str, classification: str, strides: list[int]):
    position_stride, blend_stride, texcoord_stride = strides
    position = bytearray()
    blend = bytearray()
    texcoord = bytearray()
    # FIXME: hardcoded .vb0 extension. This should be a flexible for multiple buffer export
    if not (folder / f"{name}{classification}.vb0").exists():
        return position, blend, texcoord
    with open(folder / f"{name}{classification}.vb0", "rb") as f:
        data = f.read()
        data = bytearray(data)
        i = 0
        while i < len(data):
            position += data[i : i + (position_stride)]
            blend += data[i + (position_stride) : i + (position_stride + blend_stride)]
            texcoord += data[
                i + (position_stride + blend_stride) : i
                + (position_stride + blend_stride + texcoord_stride)
            ]
            i += position_stride + blend_stride + texcoord_stride

    return position, blend, texcoord


def collect_ib(folder: Path, name: str, classification: str, offset: int):
    ib = bytearray()
    if not (folder / f"{name}{classification}.ib").exists():
        return ib
    with open(folder / f"{name}{classification}.ib", "rb") as f:
        data = f.read()
        data = bytearray(data)
        i = 0
        while i < len(data):
            ib += struct.pack("1I", struct.unpack("1I", data[i : i + 4])[0] + offset)
            i += 4
    return ib


def collect_vb_single(folder: Path, name: str, classification: str, stride: int):
    result = bytearray()
    # FIXME: harcoded vb0. This should be flexible for multiple buffer export
    if not (folder / f"{name}{classification}.vb0").exists():
        return result
    with open(folder / f"{name}{classification}.vb0", "rb") as f:
        data = f.read()
        data = bytearray(data)
        i = 0
        while i < len(data):
            result += data[i : i + stride]
            i += stride
    return result


def export_3dmigoto_xxmi(
    operator: Operator,
    context: Context,
    vb_path: Path,
    ib_path: Path,
    fmt_path: Path,
    ignore_hidden: bool,
    only_selected: bool,
    no_ramps: bool,
    credit: str,
    copy_textures: bool,
    outline_optimization: bool,
    join_meshes: bool,
    apply_modifiers_and_shapekeys: bool,
    game: GameEnum,
    destination: Optional[Path] = None,
) -> None:
    scene = bpy.context.scene
    if not destination:
        destination = vb_path.parent / f"{object_name}Mod"
    object_name = vb_path.stem
    if not [
        obj for obj in scene.objects if object_name.lower() in obj.name.lower()
    ] or not [
        file
        for file in vb_path.parent.iterdir()
        if object_name.lower() in file.name.lower()
    ]:
        raise Fatal(
            "ERROR: Cannot find match for name. Double check you are exporting as ObjectName.vb to the original data folder, that ObjectName exists in scene and that hash.json exists"
        )
    hash_data = load_hashes(vb_path.parent, vb_path.stem)
    mod_exporter: ModExporter = ModExporter(
        context,
        object_name,
        hash_data,
        ignore_hidden,
        True,
        apply_modifiers_and_shapekeys,
        only_selected,
        copy_textures,
        join_meshes,
        vb_path.parent,
        destination,
        credit=credit,
        game=game,
        operator=operator,
        outline_optimization=outline_optimization,
    )
    mod_exporter.export()
    print(f"Exported {object_name} to {destination}")


def load_hashes(path: Path, name: str, hashfile: str = "hash.json") -> list[dict]:
    parent_folder = path.parent
    if path / hashfile not in path.iterdir():
        print(
            "WARNING: Could not find hash.info in character directory. Falling back to hash_info.json"
        )
        if (parent_folder / "hash_info.json") not in parent_folder.iterdir():
            raise Fatal("Cannot find hash information, check hash.json in folder")
        # Backwards compatibility with the old hash_info.json
        with open(parent_folder / "hash_info.json", "r") as f:
            hash_data = json.load(f)
            char_hashes = [hash_data[name]]
    else:
        with open(path / hashfile, "r") as f:
            char_hashes = json.load(f)
    # TODO: Check for hash.json integrity
    return char_hashes


def generate_mod_folder(
    operator: Operator,
    path: Path,
    character_name: str,
    offsets,
    no_ramps: bool,
    delete_intermediate: bool,
    credit: str,
    copy_textures: bool,
    game: GameEnum,
    destination: Optional[Path] = None,
):
    char_hash = load_hashes(path, character_name)
    if not destination:
        destination = path / f"{character_name}Mod"
    create_mod_folder(destination)

    texture_hashes_written = {}

    for num, component in enumerate(char_hash):
        # Support for custom names was added so we need this to retain backwards compatibility
        component_name = (
            component["component_name"] if "component_name" in component else ""
        )
        # Old version used "Extra" as the third object, but I've replaced it with dress - need this for backwards compatibility
        object_classifications = (
            component["object_classifications"]
            if "object_classifications" in component
            else ["Head", "Body", "Extra"]
        )
        current_name = f"{character_name}{component_name}"

        print(f"\nWorking on {current_name}")

        if not component["draw_vb"]:
            # Components without draw vbs are texture overrides only
            # This is the path for components with only texture overrides (faces, wings, etc.)
            for i in range(len(component["object_indexes"])):
                current_object = (
                    f"{object_classifications[2]}{i - 1}"
                    if i > 2
                    else object_classifications[i]
                )
                print(f"\nTexture override only on {current_object}")
                texture_hashes = (
                    component["texture_hashes"][i]
                    if component["texture_hashes"]
                    else [{"Diffuse": "_"}, {"LightMap": "_"}]
                )
                print("Copying texture files")
                for j, texture in enumerate(texture_hashes):
                    if (
                        component["component_name"] == "Face"
                        and j > 0
                        and game == GameEnum.GenshinImpact
                    ):
                        break
                    if (
                        no_ramps
                        and texture[0] in ["ShadowRamp", "MetalMap", "DiffuseGuide"]
                    ) or texture[2] in texture_hashes_written:
                        continue
                    if copy_textures:
                        shutil.copy(
                            path
                            / f"{current_name}{current_object}{texture[0]}{texture[1]}",
                            destination
                            / f"{current_name}{current_object}{texture[0]}{texture[1]}",
                        )
                    if game in (GameEnum.ZenlessZoneZero, GameEnum.HonkaiStarRail):
                        texture_hashes_written[texture[2]] = (
                            f"{current_name}{current_object}{texture[0]}{texture[1]}"
                        )
            char_hash[num]["objects"] = []
            char_hash[num]["strides"] = []
            continue

        with open(path / f"{current_name}{object_classifications[0]}.fmt", "r") as f:
            if not component["blend_vb"]:
                strides = [x.split(": ")[1] for x in f.readlines() if "stride:" in x]
            else:
                # Parse the fmt using existing classes instead of hard coding element stride values
                fmt_layout = InputLayout()
                for line in map(str.strip, f):
                    if "stride:" in line:
                        fmt_layout.stride = int(line.split(": ")[1])
                    if line.startswith("element["):
                        fmt_layout.parse_element(f)

                position_stride, blend_stride, texcoord_stride = 0, 0, 0
                for element in fmt_layout:
                    if game == GameEnum.HonkaiImpactPart2:
                        if element.SemanticName in [
                            "POSITION",
                            "NORMAL",
                            "COLOR",
                            "TANGENT",
                        ]:
                            position_stride += element.size()
                        elif element.SemanticName in [
                            "BLENDWEIGHT",
                            "BLENDWEIGHTS",
                            "BLENDINDICES",
                        ]:
                            blend_stride += element.size()
                        elif element.SemanticName in ["TEXCOORD"]:
                            texcoord_stride += element.size()
                    else:
                        if element.SemanticName in ["POSITION", "NORMAL", "TANGENT"]:
                            position_stride += element.size()
                        elif element.SemanticName in [
                            "BLENDWEIGHT",
                            "BLENDWEIGHTS",
                            "BLENDINDICES",
                        ]:
                            blend_stride += element.size()
                        elif element.SemanticName in ["COLOR", "TEXCOORD"]:
                            texcoord_stride += element.size()

                strides = [position_stride, blend_stride, texcoord_stride]
                total = sum(strides)
                print("\tPosition Stride:", position_stride)
                print("\tBlend Stride:", blend_stride)
                print("\tTexcoord Stride:", texcoord_stride)
                print("\tStride:", total)

                assert fmt_layout.stride == total, (
                    f"ERROR: Stride mismatch between fmt and vb. fmt: {fmt_layout.stride}, vb: {strides}, file: {current_name}{object_classifications[0]}.fmt"
                )
        offset = 0
        position, blend, texcoord = bytearray(), bytearray(), bytearray()
        char_hash[num]["objects"] = []
        for i in range(len(component["object_indexes"])):
            if i + 1 > len(object_classifications):
                current_object = (
                    f"{object_classifications[-1]}{i + 2 - len(object_classifications)}"
                )
            else:
                current_object = object_classifications[i]

            print(f"\nCollecting {current_object}")
            # This is the path for components which have blend data (characters, complex weapons, etc.)
            if component["blend_vb"]:
                print("Splitting VB by buffer type, merging body parts")
                try:
                    x, y, z = collect_vb(
                        path,
                        current_name,
                        current_object,
                        (position_stride, blend_stride, texcoord_stride),
                    )
                except FileNotFoundError:
                    raise Fatal(
                        f"ERROR: Unable to find {current_name}{current_object} when exporting. Double check the object exists and is named correctly"
                    )
                position += x
                blend += y
                texcoord += z
            # This is the path for components without blend data (simple weapons, objects, etc.)
            # Simplest route since we do not need to split up the buffer into multiple components
            else:
                position += collect_vb_single(
                    path, current_name, current_object, int(strides[0])
                )
                position_stride = int(strides[0])

            print("Collecting IB")
            print(f"{current_name}{current_object} offset: {offset}")
            ib = collect_ib(path, current_name, current_object, offset)

            with open(destination / f"{current_name}{current_object}.ib", "wb") as f:
                f.write(ib)
            if delete_intermediate:
                # FIXME: harcoded .vb0 extension. This should be a flexible for multiple buffer export
                os.remove(path / f"{current_name}{current_object}.vb0")
                os.remove(path / f"{current_name}{current_object}.ib")
                os.remove(path / f"{current_name}{current_object}.fmt")

            if len(position) % position_stride != 0:
                print("ERROR: VB buffer length does not match stride")

            char_hash[num]["objects"].append(
                {
                    "fullname": f"{current_name}{current_object}",
                    "offsets": offsets[current_name + current_object],
                }
            )

            offset += len(position) // position_stride

            # Older versions can only manage diffuse and lightmaps
            texture_hashes = (
                component["texture_hashes"][i]
                if "texture_hashes" in component
                else [["Diffuse", ".dds", "_"], ["LightMap", ".dds", "_"]]
            )

            print("Copying texture files")
            for j, texture in enumerate(texture_hashes):
                if (
                    no_ramps
                    and texture[0] in ["ShadowRamp", "MetalMap", "DiffuseGuide"]
                ) or texture[2] in texture_hashes_written:
                    continue
                if copy_textures:
                    shutil.copy(
                        path
                        / f"{current_name}{current_object}{texture[0]}{texture[1]}",
                        destination
                        / f"{current_name}{current_object}{texture[0]}{texture[1]}",
                    )
                if game in (GameEnum.ZenlessZoneZero, GameEnum.HonkaiStarRail):
                    texture_hashes_written[texture[2]] = (
                        f"{current_name}{current_object}{texture[0]}{texture[1]}"
                    )

        char_hash[num]["total_verts"] = offset
        char_hash[num]["strides"] = strides
        if component["blend_vb"]:
            print("Writing merged buffer files")
            with (
                open(destination / f"{current_name}Position.buf", "wb") as f,
                open(destination / f"{current_name}Blend.buf", "wb") as g,
                open(destination / f"{current_name}Texcoord.buf", "wb") as h,
            ):
                f.write(position)
                g.write(blend)
                h.write(texcoord)
        else:
            with open(destination / f"{current_name}.buf", "wb") as f:
                f.write(position)

    #    setup
    #    for each component:
    #        splitting buffers(main_buffer) -> total_vert_coints, split_buffers(pos,blend,tex)
    #        for object_classif:
    #            moving files into mod folder(hash_json)
    #            - filtered_textures
    #            - ib
    #            - split vbs
    #            - filtered_resources(each point to a file)

    print("Generating .ini file")
    ini_data = generate_ini(
        character_name,
        char_hash,
        offsets,
        texture_hashes_written,
        credit,
        game,
        operator,
    )
    if not ini_data:
        raise Fatal(
            "ERROR: Could not generate ini file. Install dependencies from settings"
        )
    with open(destination / f"{character_name}.ini", "w", encoding="UTF-8") as f:
        print("Writing ini file")
        f.write(ini_data)
    print("All operations completed, exiting")


def blender_to_migoto_vertices(
    operator: Operator,
    mesh: Mesh,
    obj: Object,
    fmt_layout: InputLayout,
    game: GameEnum,
    translate_normal: bool,
    translate_tangent: bool,
    main_obj: Object,
    outline_properties=None,
):
    dtype = numpy.dtype([])
    for elem in fmt_layout:
        # Numpy Future warning: 1 vs (1,) shape.
        # Doesn't cause issues right now but might in a future blender update.
        if elem.InputSlotClass != "per-vertex" or elem.reused_offset:
            continue
        if f32_pattern.match(elem.Format):
            dtype = numpy.dtype(
                dtype.descr + [(elem.name, (numpy.float32, elem.format_len))]
            )
        elif f16_pattern.match(elem.Format):
            dtype = numpy.dtype(
                dtype.descr + [(elem.name, (numpy.float16, elem.format_len))]
            )
        elif u32_pattern.match(elem.Format):
            dtype = numpy.dtype(
                dtype.descr + [(elem.name, (numpy.uint32, elem.format_len))]
            )
        elif u16_pattern.match(elem.Format):
            dtype = numpy.dtype(
                dtype.descr + [(elem.name, (numpy.uint16, elem.format_len))]
            )
        elif u8_pattern.match(elem.Format):
            dtype = numpy.dtype(
                dtype.descr + [(elem.name, (numpy.uint8, elem.format_len))]
            )
        elif s32_pattern.match(elem.Format):
            dtype = numpy.dtype(
                dtype.descr + [(elem.name, (numpy.int32, elem.format_len))]
            )
        elif s16_pattern.match(elem.Format):
            dtype = numpy.dtype(
                dtype.descr + [(elem.name, (numpy.int16, elem.format_len))]
            )
        elif s8_pattern.match(elem.Format):
            dtype = numpy.dtype(
                dtype.descr + [(elem.name, (numpy.int8, elem.format_len))]
            )
        elif unorm16_pattern.match(elem.Format):
            dtype = numpy.dtype(
                dtype.descr + [(elem.name, (numpy.uint16, elem.format_len))]
            )
        elif unorm8_pattern.match(elem.Format):
            dtype = numpy.dtype(
                dtype.descr + [(elem.name, (numpy.uint8, elem.format_len))]
            )
        elif snorm16_pattern.match(elem.Format):
            dtype = numpy.dtype(
                dtype.descr + [(elem.name, (numpy.int16, elem.format_len))]
            )
        elif snorm8_pattern.match(elem.Format):
            dtype = numpy.dtype(
                dtype.descr + [(elem.name, (numpy.int8, elem.format_len))]
            )
        else:
            raise Fatal("File uses an unsupported DXGI Format: %s" % elem.Format)
    migoto_verts = numpy.zeros(len(mesh.loops), dtype=dtype)
    if len(mesh.polygons) == 0:
        return migoto_verts, dtype
    weights_error_flag = -1

    masked_vgs = [vg.index for vg in obj.vertex_groups if vg.name.startswith("MASK")]
    bm = bmesh.new()
    bm.from_mesh(mesh)
    layer_deform = bm.verts.layers.deform.active
    weights = None
    if layer_deform is not None:
        bm.verts.ensure_lookup_table()
        weights = [
            {
                index: weight
                for index, weight in v[layer_deform].items()
                if index not in masked_vgs
            }
            for v in bm.verts
        ]
        weights = [
            dict(sorted(w.items(), key=lambda item: item[1], reverse=True))
            for w in weights
        ]
    weights_np = None
    blend_len = None
    debug_text = "\t--TIME PER ELEMENT--\n"
    start_timer = time.time()
    idxs = [loop.index for loop in mesh.loops]
    verts = [loop.vertex_index for loop in mesh.loops]
    for elem in fmt_layout:
        start_timer_short = time.time()
        if elem.InputSlotClass != "per-vertex" or elem.reused_offset:
            continue
        semantic_translations = fmt_layout.get_semantic_remap()
        translated_elem_name, translated_elem_index = semantic_translations.get(
            elem.name, (elem.name, elem.SemanticIndex)
        )
        if translated_elem_name == "POSITION":
            position = numpy.zeros(len(mesh.vertices), dtype=(numpy.float32, 3))
            mesh.vertices.foreach_get("undeformed_co", position.ravel())
            result = numpy.ones(len(mesh.loops), dtype=(numpy.float32, elem.format_len))
            result[idxs, 0:3] = position[verts]
            result[idxs, 0] *= -(2 * main_obj.get("3DMigoto:FlipMesh", False) - 1)
            if "POSITION.w" in custom_attributes_float(mesh):
                loop_position_w = numpy.ones(
                    len(mesh.loops), dtype=(numpy.float16, (1,))
                )
                loop_position_w[idxs] = (
                    custom_attributes_float(mesh)["POSITION.w"].data[verts].value
                )
                result[idxs, 3] = loop_position_w[verts, 3]
        elif translated_elem_name == "NORMAL":
            normal = numpy.zeros(len(mesh.loops), dtype=(numpy.float16, 3))
            mesh.loops.foreach_get("normal", normal.ravel())
            result = numpy.zeros(
                len(mesh.loops), dtype=(numpy.float16, elem.format_len)
            )
            result[:, 0:3] = normal
            result[idxs, 0] *= -(2 * main_obj.get("3DMigoto:FlipMesh", False) - 1)
            if "NORMAL.w" in custom_attributes_float(mesh):
                loop_normal_w = numpy.zeros(
                    len(mesh.loops), dtype=(numpy.float16, (1,))
                )
                for loop in mesh.loops:
                    loop_normal_w[loop.index] = (
                        custom_attributes_float(mesh)["NORMAL.w"]
                        .data[loop.vertex_index]
                        .value
                    )
                result[:, 3] = loop_normal_w
            if main_obj.get(
                "3DMigoto:FlipNormal", False
            ):  # This flips and converts normals to UNORM if needed
                result = -result
            if elem.Format.upper().endswith("_UNORM"):
                result = result * 2 - 1
        elif translated_elem_name.startswith("TANGENT"):
            temp_tangent = numpy.zeros((len(mesh.loops), 3), dtype=numpy.float16)
            bitangent_sign = numpy.zeros(len(mesh.loops), dtype=numpy.float16)
            result = numpy.zeros((len(mesh.loops), 4), dtype=numpy.float16)
            mesh.loops.foreach_get("tangent", temp_tangent.ravel())
            mesh.loops.foreach_get("bitangent_sign", bitangent_sign)
            if outline_properties[0]:
                export_outline = optimized_outline_generation(
                    obj, mesh, outline_properties
                )
                for loop in mesh.loops:
                    temp_tangent[loop.index] = export_outline.get(
                        loop.vertex_index, temp_tangent[loop.index]
                    )
            elif game == GameEnum.GenshinImpact:
                mesh.loops.foreach_get("normal", temp_tangent.ravel())
            if main_obj.get(
                "3DMigoto:FlipNormal", False
            ):  # This flips and converts tangent to UNORM if needed
                temp_tangent = -temp_tangent
            if elem.Format.upper().endswith("_UNORM"):
                temp_tangent = temp_tangent * 2 - 1
            result[:, 0:3] = temp_tangent
            result[:, 3] = bitangent_sign
            if game == GameEnum.ZenlessZoneZero:
                result[:, 3] = -result[:, 3]
            result = result[:, 0 : elem.format_len]
            result[:, 0] *= -(2 * main_obj.get("3DMigoto:FlipMesh", False) - 1)
        elif translated_elem_name.startswith("BLENDWEIGHT"):
            if weights_np is None:
                if weights is None:
                    # TODO: Meshes without weights should export. Remove this fatal error.
                    raise Fatal(
                        "The export format is expecting weights and the model has none. Aborting."
                    )
                assert blend_len is None or blend_len == elem.format_len
                blend_len = elem.format_len
                weights_np = numpy.zeros(
                    len(mesh.vertices),
                    dtype=numpy.dtype(
                        [
                            ("INDEX", numpy.int32, elem.format_len),
                            ("WEIGHT", numpy.float32, elem.format_len),
                        ]
                    ),
                )
                for vert_idx, vert in enumerate(weights):
                    for i, (g, w) in enumerate(vert.items()):
                        try:
                            if elem.format_len > 1:
                                weights_np["INDEX"][vert_idx][i] = g
                                weights_np["WEIGHT"][vert_idx][i] = w
                            else:
                                weights_np["INDEX"][vert_idx] = g
                                weights_np["WEIGHT"][vert_idx] = w
                        except IndexError:
                            continue
            result = numpy.zeros(
                len(mesh.loops), dtype=(numpy.float32, elem.format_len)
            )
            result[idxs] = weights_np["WEIGHT"][verts]
            if operator.normalize_weights:
                if elem.format_len > 1:
                    result = result / numpy.sum(result, axis=1)[:, None]
                else:
                    result = result / result
        elif translated_elem_name.startswith("BLENDINDICES"):
            if weights_np is None:
                if weights is None:
                    raise Fatal(
                        "The export format is expecting weights and the model has none. Aborting."
                    )
                assert blend_len is None or blend_len == elem.format_len
                blend_len = elem.format_len
                weights_np = numpy.zeros(
                    len(mesh.vertices),
                    dtype=numpy.dtype(
                        [
                            ("INDEX", numpy.int32, elem.format_len),
                            ("WEIGHT", numpy.float32, elem.format_len),
                        ]
                    ),
                )
                for vert_idx, vert in enumerate(weights):
                    for i, (g, w) in enumerate(vert.items()):
                        try:
                            if elem.format_len > 1:
                                weights_np["INDEX"][vert_idx][i] = g
                                weights_np["WEIGHT"][vert_idx][i] = w
                            else:
                                weights_np["INDEX"][vert_idx] = g
                                weights_np["WEIGHT"][vert_idx] = w
                        except IndexError:
                            continue
            result = numpy.zeros(
                len(mesh.loops), dtype=(numpy.float32, elem.format_len)
            )
            result[idxs] = weights_np["INDEX"][verts]
        elif translated_elem_name.startswith("COLOR"):
            result = numpy.zeros(len(mesh.loops), dtype=(numpy.float32, 4))
            mesh.vertex_colors[elem.name].data.foreach_get("color", result.ravel())
            result = result[:, 0 : elem.format_len]
        elif translated_elem_name.startswith("TEXCOORD") and elem.is_float():
            result = numpy.zeros(
                len(mesh.loops), dtype=(numpy.float32, elem.format_len)
            )
            count = 0
            for uv in (f"{elem.name}.xy", f"{elem.name}.zw"):
                if uv in mesh.uv_layers:
                    temp_uv = numpy.zeros(len(mesh.loops), dtype=(numpy.float32, 2))
                    mesh.uv_layers[uv].data.foreach_get("uv", temp_uv.ravel())
                    try:
                        if main_obj["3DMigoto:" + uv]["flip_v"]:
                            temp_uv[:, 1] = 1.0 - temp_uv[:, 1]
                    except KeyError:
                        pass
                    result[:, count : count + 2] = temp_uv
                    count += 2
            for uv in (f"{elem.name}.x", f"{elem.name}.z"):
                if uv in mesh.uv_layers:
                    temp_uv = numpy.zeros(len(mesh.loops), dtype=(numpy.float32, 2))
                    mesh.uv_layers[uv].data.foreach_get("uv", result[uv].ravel())
                    temp_uv = temp_uv[:, 0]
                    result[:, count] = temp_uv
        else:
            # Unhandled semantics are saved in vertex layers
            if elem.is_float():
                result = numpy.zeros(
                    len(mesh.loops), dtype=(numpy.float32, (elem.format_len,))
                )
            elif elem.is_int():
                result = numpy.zeros(
                    len(mesh.loops), dtype=(numpy.int32, (elem.format_len,))
                )
            else:
                print("Warning: Unhandled semantic %s %s" % (elem.name, elem.Format))
            for i, component in enumerate("xyzw"):
                if i >= elem.format_len:
                    break
                layer_name = "%s.%s" % (elem.name, component)
                if layer_name in custom_attributes_int(mesh):
                    for loop in mesh.loops:
                        result[:, i][loop.index] = (
                            custom_attributes_int(mesh)[layer_name]
                            .data[loop.vertex_index]
                            .value
                        )
                elif layer_name in custom_attributes_float(mesh):
                    for loop in mesh.loops:
                        result[:, i][loop.index] = (
                            custom_attributes_float(mesh)[layer_name]
                            .data[loop.vertex_index]
                            .value
                        )
        if not translated_elem_name.startswith("BLENDINDICES"):
            if unorm16_pattern.match(elem.Format):
                result = numpy.round(result * 65535).astype(numpy.uint16)
            elif unorm8_pattern.match(elem.Format):
                result = numpy.round(result * 255).astype(numpy.uint8)
            elif snorm16_pattern.match(elem.Format):
                result = numpy.round(result * 32767).astype(numpy.int16)
            elif snorm8_pattern.match(elem.Format):
                result = numpy.round(result * 127).astype(numpy.int8)
        migoto_verts[elem.name] = result
        debug_text += f"\t{elem.name:>12}: {(time.time() - start_timer_short):.5f}\n"
    if weights_error_flag != -1:
        debug_text += f"Warning: Mesh: {obj.name} has more than {weights_error_flag} blend weights or indices per vertex. The extra weights or indices will be ignored.\n"
    debug_text += f"\tMigoto verts took {(time.time() - start_timer):.5f} seconds"
    print(debug_text)
    bm.free()
    return migoto_verts, dtype


def mesh_to_bin(
    context: Context,
    operator: Operator,
    obj: Object,
    migoto_format: MigotoFmt,
    game: GameEnum,
    translate_normal: numpy.vectorize,
    translate_tangent: numpy.vectorize,
    main_obj: Object,
    outline_properties,
) -> tuple[NumpyBuffer, NumpyBuffer]:
    vb: NumpyBuffer = NumpyBuffer(migoto_format.vb_layout)
    ib: NumpyBuffer = NumpyBuffer(migoto_format.ib_layout)
    if len(obj.data.polygons) == 0:
        print("\tMesh has no polygons, skipping")
        return ib, vb

    # Calculates tangents and makes loop normals valid (still with our custom normal data from import time):
    mesh: Mesh = (
        apply_modifiers_and_shapekeys(context, obj)
        if operator.apply_modifiers_and_shapekeys
        else obj.to_mesh()
    )
    if main_obj != obj:
        # Matrix world seems to be the summatory of all transforms parents included
        # Might need to test for more edge cases and to confirm these suspicious,
        # other available options: matrix_local, matrix_basis, matrix_parent_inverse
        mesh.transform(obj.matrix_world)
        mesh.transform(main_obj.matrix_world.inverted())
    mesh.update()
    mesh_triangulate(mesh)
    try:
        mesh.calc_tangents()
    except RuntimeError:
        raise Fatal(
            "ERROR: Unable to find UV map. Double check UV map exists and is called TEXCOORD.xy"
        )
    start_timer = time.time()
    migoto_verts, dtype = blender_to_migoto_vertices(
        operator,
        mesh,
        obj,
        migoto_format,
        game,
        translate_normal,
        translate_tangent,
        main_obj,
        outline_properties,
    )

    ibvb_timer = time.time()
    indexed_vertices = collections.OrderedDict()
    ib_list = [
        indexed_vertices.setdefault(
            migoto_verts[l.vertex_index].tobytes(), len(indexed_vertices)
        )
        for l in mesh.loops
    ]
    ib.fromlist(ib_list)

    # Bitwise XOR. We are assuming these values would always be boolean.
    # It might need more sanity checks in the future.
    if main_obj.get("3DMigoto:FlipMesh", False) ^ main_obj.get(
        "3DMigoto:FlipWinding", False
    ):
        ib.data = numpy.fliplr(ib.data)
    print(
        f"\t\tIB GEN: {time.time() - ibvb_timer:.5f}, {len(ib)},{len(mesh.loops) // 3}"
    )

    ibvb_timer = time.time()
    vb_bytes = bytearray()
    for vertex in indexed_vertices:
        vb_bytes += bytes(vertex)

    vb.data = numpy.frombuffer(vb_bytes, dtype=vb.get_numpy_type())

    print(f"\t\tVB GEN: {time.time() - ibvb_timer:.5f}")
    print(
        f"\tMesh to bin generated {len(vb)} vertex in {time.time() - start_timer:.5f} seconds"
    )
    obj.to_mesh_clear()
    obj.data.update()
    return ib, vb


def shapekey_generation(obj: Object, mesh: Mesh):
    sk_dtype = numpy.dtype(
        [
            ("VERTEX_INDEX", numpy.uint32),
            ("POSITION", numpy.float32, 3),
        ]
    )
    sk_datas = []
    for i, sk in enumerate(mesh.shape_keys.key_blocks):
        print(f"Processing shapekey {sk.name}")
        sk_data = numpy.zeros(len(mesh.vertices), dtype=sk_dtype)
        pos = numpy.zeros(len(mesh.vertices), dtype=(numpy.float32, 3))
        sk_data["VERTEX_INDEX"] = numpy.array(
            range(len(mesh.vertices)), dtype=(numpy.uint32)
        )
        sk.data.foreach_get("co", pos.ravel())
        sk_data["POSITION"] = pos
        if i != 0:  # skip Basis
            sk_data = numpy.delete(
                sk_data,
                numpy.nonzero(
                    numpy.all(sk_data["POSITION"] == sk_datas[0]["POSITION"], axis=1)
                ),
                axis=0,
            )
        sk_datas.append(sk_data)
    total = sum(len(sk_data) for sk_data in sk_datas)
    shapekey_buff = numpy.zeros(total, dtype=sk_dtype)
    offset_count = []
    offset = 0
    for i, sk_data in enumerate(sk_datas):
        if i == 0:  # skip Basis
            continue
        offset_count.append(offset)
        shapekey_buff[offset : offset + len(sk_data)] = sk_data
        offset += len(sk_data)
    return offset_count, shapekey_buff


class Export3DMigoto(Operator, ExportHelper):
    """Export a mesh for re-injection into a game with 3DMigoto"""

    bl_idname = "export_mesh.migoto"
    bl_label = "Export 3DMigoto Vertex & Index Buffers"

    filename_ext = ".vb0"
    filter_glob: StringProperty(
        default="*.vb*",
        options={"HIDDEN"},
    )

    def invoke(self, context, event):
        return ExportHelper.invoke(self, context, event)

    def execute(self, context):
        try:
            file_path = Path(self.filepath)
            vb_path = file_path.parent / file_path.stem + ".vb"
            ib_path = file_path.parent / file_path.stem + ".ib"
            fmt_path = file_path.parent / file_path.stem + ".fmt"
            ini_path = file_path.parent / file_path.stem + "_generated.ini"
            obj = context.object
            self.flip_normal = obj.get("3DMigoto:FlipNormal", False)
            self.flip_tangent = obj.get("3DMigoto:FlipTangent", False)
            self.flip_winding = obj.get("3DMigoto:FlipWinding", False)
            self.flip_mesh = obj.get("3DMigoto:FlipMesh", False)
            # FIXME: ExportHelper will check for overwriting vb_path, but not ib_path
            export_3dmigoto(self, context, vb_path, ib_path, fmt_path, ini_path)
        except Fatal as e:
            self.report({"ERROR"}, str(e))
        return {"FINISHED"}


class Export3DMigotoXXMI(Operator, ExportHelper):
    """Export a mesh for re-injection into a game with 3DMigoto"""

    bl_idname = "export_mesh_xxmi.migoto"
    bl_label = "Export mod folder"
    bl_options = {"PRESET", "UNDO"}

    filename_ext = ".vb*"
    filter_glob: StringProperty(
        default="*.vb*",
        options={"HIDDEN"},
    )

    ignore_hidden: BoolProperty(
        name="Ignore hidden objects",
        description="Does not use objects in the Blender window that are hidden while exporting mods",
        default=True,
    )

    only_selected: BoolProperty(
        name="Only export selected",
        description="Uses only the selected objects when deciding which meshes to export",
        default=False,
    )

    no_ramps: BoolProperty(
        name="Ignore shadow ramps/metal maps/diffuse guide",
        description="Skips exporting shadow ramps, metal maps and diffuse guides",
        default=True,
    )

    copy_textures: BoolProperty(
        name="Copy textures",
        description="Copies the texture files to the mod folder, useful for the initial export but might be redundant afterwards.",
        default=True,
    )

    credit: StringProperty(
        name="Credit",
        description="Name that pops up on screen when mod is loaded. If left blank, will result in no pop up",
        default="",
    )

    game: EnumProperty(
        name="Game to mod",
        description="Select the game you are modding to optimize the mod for that game",
        items=game_enum,
    )
    apply_modifiers_and_shapekeys: BoolProperty(
        name="Apply modifiers and shapekeys",
        description="Applies shapekeys and modifiers(unless marked MASK); then joins meshes to a single object. The criteria to join is as follows, the objects imported from dump are considered containers; collections starting with their same name are going to be joint into said containers",
        default=False,
    )
    join_meshes: BoolProperty(
        name="Join meshes",
        description="Joins all meshes into a single object. Allows for versatile pre-baked animation mods and blender like masking for toggles.",
        default=False,
    )
    normalize_weights: BoolProperty(
        name="Normalize weights to format",
        description="Limits weights to match export format. Also normalizes the remaining weights",
        default=False,
    )
    outline_optimization: BoolProperty(
        name="Outline Optimization",
        description="Recalculate outlines. Recommended for final export. Check more options below to improve quality. This option is tailored for Genshin Impact and may not work as well for other games. Use with caution.",
        default=False,
    )
    apply_modifiers_and_shapekeys: BoolProperty(
        name="Apply modifiers and shapekeys",
        description="Applies shapekeys and modifiers(unless marked MASK); then joins meshes to a single object. The criteria to join is as follows, the objects imported from dump are considered containers; collections starting with their same name are going to be joint into said containers",
        default=False,
    )
    join_meshes: BoolProperty(
        name="Join meshes",
        description="Joins all meshes into a single object. Allows for versatile pre-baked animation mods and blender like masking for toggles.",
        default=False,
    )
    export_shapekeys: BoolProperty(
        name="Export shape keys",
        description="Exports marked shape keys for the selected object. Also generates the necessary sections in ini file",
        default=False,
    )

    def draw(self, context):
        layout = self.layout
        col = layout.column(align=True)
        col.prop(self, "game")
        col.prop(self, "ignore_hidden")
        col.prop(self, "only_selected")
        col.prop(self, "no_ramps")
        col.prop(self, "copy_textures")
        col.prop(self, "apply_modifiers_and_shapekeys")
        col.prop(self, "join_meshes")
        col.prop(self, "normalize_weights")
        # col.prop(self, 'export_shapekeys')
        layout.separator()
        col.prop(self, "outline_optimization")

        if self.outline_optimization:
            col.prop(
                self,
                "toggle_rounding_outline",
                text="Vertex Position Rounding",
                toggle=True,
                icon="SHADING_WIRE",
            )
            col.prop(self, "decimal_rounding_outline")
            if self.toggle_rounding_outline:
                col.prop(self, "detect_edges")
            if self.detect_edges and self.toggle_rounding_outline:
                col.prop(self, "nearest_edge_distance")
            col.prop(self, "overlapping_faces")
            col.prop(self, "angle_weighted")
            col.prop(self, "calculate_all_faces")
        layout.separator()

        col.prop(self, "credit")

    def invoke(self, context, event):
        obj = context.object
        if obj is None:
            try:
                obj = [
                    obj
                    for obj in bpy.data.objects
                    if obj.type == "MESH" and obj.visible_get()
                ][0]
            except IndexError:
                return ExportHelper.invoke(self, context, event)
        return ExportHelper.invoke(self, context, event)

    def execute(self, context):
        try:
            vb_path = Path(self.filepath)
            ib_path = vb_path.parent / (vb_path.stem + ".ib")
            fmt_path = vb_path.parent / (vb_path.stem + ".fmt")
            # FIXME: ExportHelper will check for overwriting vb_path, but not ib_path
            export_3dmigoto_xxmi(
                self,
                context,
                vb_path,
                ib_path,
                fmt_path,
                self.ignore_hidden,
                self.only_selected,
                self.no_ramps,
                self.credit,
                self.copy_textures,
                self.outline_optimization,
                self.apply_modifiers_and_shapekeys,
                self.join_meshes,
                self.game,
            )
            self.report({"INFO"}, "Export completed")
        except Fatal as e:
            self.report({"ERROR"}, str(e))
        return {"FINISHED"}


class XXMIProperties(PropertyGroup):
    """Properties for XXMITools"""

    destination_path: StringProperty(
        name="Output Folder",
        description="Output Folder:",
        default="",
        maxlen=1024,
    )
    dump_path: StringProperty(
        name="Dump Folder",
        description="Dump Folder:",
        default="",
        maxlen=1024,
    )
    filter_glob: StringProperty(
        default="*.vb*",
        options={"HIDDEN"},
    )

    flip_winding: BoolProperty(
        name="Flip Winding Order",
        description="Flip winding order during export (automatically set to match the import option)",
        default=False,
    )

    ignore_hidden: BoolProperty(
        name="Ignore hidden objects",
        description="Does not use objects in the Blender window that are hidden while exporting mods",
        default=True,
    )

    only_selected: BoolProperty(
        name="Only export selected",
        description="Uses only the selected objects when deciding which meshes to export",
        default=False,
    )

    no_ramps: BoolProperty(
        name="Ignore shadow ramps/metal maps/diffuse guide",
        description="Skips exporting shadow ramps, metal maps and diffuse guides",
        default=True,
    )

    copy_textures: BoolProperty(
        name="Copy textures",
        description="Copies the texture files to the mod folder, useful for the initial export but might be redundant afterwards",
        default=True,
    )

    credit: StringProperty(
        name="Credit",
        description="Name that pops up on screen when mod is loaded. If left blank, will result in no pop up",
        default="",
    )

    outline_optimization: BoolProperty(
        name="Outline Optimization",
        description="Recalculate outlines. Recommended for final export. Check more options below to improve quality",
        default=False,
    )

    toggle_rounding_outline: BoolProperty(
        name="Round vertex positions",
        description="Rounding of vertex positions to specify which are the overlapping vertices",
        default=True,
    )

    decimal_rounding_outline: IntProperty(
        name="Decimals:",
        description="Rounding of vertex positions to specify which are the overlapping vertices",
        default=3,
    )

    angle_weighted: BoolProperty(
        name="Weight by angle",
        description="Optional: calculate angles to improve accuracy of outlines. Slow",
        default=False,
    )

    overlapping_faces: BoolProperty(
        name="Ignore overlapping faces",
        description="Detect and ignore overlapping/antiparallel faces to avoid buggy outlines",
        default=False,
    )

    detect_edges: BoolProperty(
        name="Calculate edges",
        description="Calculate for disconnected edges when rounding, closing holes in the edge outline",
        default=False,
    )

    calculate_all_faces: BoolProperty(
        name="Calculate outline for all faces",
        description="Calculate outline for all faces, which is especially useful if you have any flat shaded non-edge faces. Slow",
        default=False,
    )

    nearest_edge_distance: FloatProperty(
        name="Distance:",
        description="Expand grouping for edge vertices within this radial distance to close holes in the edge outline. Requires rounding",
        default=0.001,
        min=0.001,
    )
    game: EnumProperty(
        name="Game to mod",
        description="Select the game you are modding to optimize the mod for that game",
        items=game_enum,
    )
    apply_modifiers_and_shapekeys: BoolProperty(
        name="Apply modifiers and shapekeys",
        description="Applies shapekeys and modifiers(unless marked MASK); then joins meshes to a single object. The criteria to join is as follows, the objects imported from dump are considered containers; collections starting with their same name are going to be joint into said containers",
        default=False,
    )
    join_meshes: BoolProperty(
        name="Join meshes",
        description="Joins all meshes into a single object. Allows for versatile pre-baked animation mods and blender like masking for toggles.",
        default=False,
    )
    normalize_weights: BoolProperty(
        name="Normalize weights to format",
        description="Limits weights to match export format. Also normalizes the remaining weights",
        default=False,
    )
    export_shapekeys: BoolProperty(
        name="Export shape keys",
        description="Exports marked shape keys for the selected object. Also generates the necessary sections in ini file",
        default=False,
    )
    batch_pattern: StringProperty(
        name="Batch pattern",
        description="Pattern to name export folders. Example: name_###",
        default="",
    )


class DestinationSelector(Operator, ExportHelper):
    """Export single mod based on current frame"""

    bl_idname = "destination.selector"
    bl_label = "Destination"
    filename_ext = "."
    use_filter_folder = True
    filter_glob: StringProperty(
        default=".",
        options={"HIDDEN"},
    )

    def execute(self, context):
        userpath = Path(self.properties.filepath)
        if not userpath.is_dir():
            userpath = userpath.parent
        context.scene.xxmi.destination_path = str(userpath)
        bpy.ops.ed.undo_push(message="XXMI Tools: destination selected")
        return {"FINISHED"}


class DumpSelector(Operator, ExportHelper):
    """Export single mod based on current frame"""

    bl_idname = "dump.selector"
    bl_label = "Dump folder selector"
    filename_ext = "."
    use_filter_folder = True
    filter_glob: StringProperty(
        default=".",
        options={"HIDDEN"},
    )

    def execute(self, context):
        userpath = Path(self.properties.filepath)
        self.properties.filepath = str(userpath.parent)
        context.scene.xxmi.dump_path = str(userpath.parent)
        bpy.ops.ed.undo_push(message="XXMI Tools: dump path selected")
        return {"FINISHED"}


class ExportAdvancedOperator(Operator):
    """Export operation base class"""

    bl_idname = "xxmi.exportadvanced"
    bl_label = "Export Mod"
    bl_description = "Export mod"
    bl_options = {"REGISTER"}
    operations = []

    def execute(self, context):
        scene = bpy.context.scene
        xxmi = scene.xxmi
        if not xxmi.dump_path:
            self.report({"ERROR"}, "Dump path not set")
            return {"CANCELLED"}
        if not xxmi.destination_path:
            self.report({"ERROR"}, "Destination path not set")
            return {"CANCELLED"}
        if xxmi.destination_path == xxmi.dump_path:
            self.report({"ERROR"}, "Destination path can not be the same as Dump path")
            return {"CANCELLED"}
        try:
            base_path = Path(xxmi.dump_path + "/")
            vb_path = base_path / (base_path.stem + ".vb")
            ib_path = base_path / (base_path.stem + ".ib")
            fmt_path = base_path / (base_path.stem + ".fmt")
            # FIXME: ExportHelper will check for overwriting vb_path, but not ib_path
            game = GameEnum[xxmi.game]
            start = time.time()
            export_3dmigoto_xxmi(
                self,
                context,
                vb_path,
                ib_path,
                fmt_path,
                xxmi.ignore_hidden,
                xxmi.only_selected,
                xxmi.no_ramps,
                xxmi.credit,
                xxmi.copy_textures,
                xxmi.outline_optimization,
                xxmi.apply_modifiers_and_shapekeys,
                xxmi.join_meshes,
                game,
                Path(xxmi.destination_path),
            )
            print("Export took", time.time() - start, "seconds")
            self.report({"INFO"}, "Export completed")
        except Fatal as e:
            self.report({"ERROR"}, str(e))
        return {"FINISHED"}


class ExportAdvancedBatchedOperator(Operator):
    """Export operation base class"""

    bl_idname = "xxmi.exportadvancedbatched"
    bl_label = "Batch export"
    bl_description = "Exports 1 mod per frame of blender timeline as a single mod. Folder names follow the pattern specified in the batch pattern"
    bl_options = {"REGISTER"}
    operations = []

    def invoke(self, context, event):
        scene = bpy.context.scene
        if bpy.app.version < (4, 1, 0):
            return context.window_manager.invoke_confirm(operator=self, event=event)
        return context.window_manager.invoke_confirm(
            operator=self,
            event=event,
            message=f"Exporting {scene.frame_end + 1 - scene.frame_start} copies of the mod. This may take a while. Continue?",
            title="Batch export",
            icon="WARNING",
            confirm_text="Continue",
        )

    def execute(self, context):
        scene = bpy.context.scene
        xxmi = scene.xxmi
        start_time = time.time()
        base_dir = Path(xxmi.destination_path)
        wildcards = ("#####", "####", "###", "##", "#")
        try:
            for frame in range(scene.frame_start, scene.frame_end + 1):
                context.scene.frame_set(frame)
                for w in wildcards:
                    frame_folder = Path(
                        xxmi.batch_pattern.replace(w, str(frame).zfill(len(w)))
                    )
                    if frame_folder != xxmi.batch_pattern:
                        break
                else:
                    self.report(
                        {"ERROR"},
                        "Batch pattern must contain any number of # wildcard characters for the frame number to be written into it. Example name_### -> name_001",
                    )
                    return False
                xxmi.destination_path = base_dir / frame_folder
                bpy.ops.xxmi.exportadvanced()
                print(
                    f"Exported frame {frame + 1 - scene.frame_start}/{scene.frame_end + 1 - scene.frame_start}"
                )
            print(f"Batch export took {time.time() - start_time} seconds")
        except Fatal as e:
            self.report({"ERROR"}, str(e))
        xxmi.destination_path = base_dir
        return {"FINISHED"}


def write_fmt_file(f, vb: VertexBufferGroup, ib: IndexBuffer, strides: list[int]):
    for vbuf_idx, stride in strides.items():
        if vbuf_idx.isnumeric():
            f.write("vb%s stride: %i\n" % (vbuf_idx, stride))
        else:
            f.write("stride: %i\n" % stride)
    f.write("topology: %s\n" % vb.topology)
    if ib is not None:
        f.write("format: %s\n" % ib.format)
    f.write(vb.layout.to_string())


def write_ini_file(
    f,
    vb: VertexBufferGroup,
    vb_path,
    ib: IndexBuffer,
    ib_path,
    strides: list[int],
    obj: Object,
    topology: str,
):
    backup = True
    # topology='trianglestrip' # Testing
    bind_section = ""
    backup_section = ""
    restore_section = ""
    resource_section = ""
    resource_bak_section = ""

    draw_section = "handling = skip\n"
    if ib is not None:
        draw_section += "drawindexed = auto\n"
    else:
        draw_section += "draw = auto\n"

    if ib is not None:
        bind_section += "ib = ResourceIB\n"
        resource_section += textwrap.dedent("""
            [ResourceIB]
            type = buffer
            format = {}
            filename = {}
            """).format(ib.format, ib_path)
        if backup:
            resource_bak_section += "[ResourceBakIB]\n"
            backup_section += "ResourceBakIB = ref ib\n"
            restore_section += "ib = ResourceBakIB\n"

    for vbuf_idx, stride in strides.items():
        bind_section += "vb{0} = ResourceVB{0}\n".format(vbuf_idx or 0)
        resource_section += textwrap.dedent("""
            [ResourceVB{}]
            type = buffer
            stride = {}
            filename = {}
            """).format(vbuf_idx, stride, vb_path + vbuf_idx)
        if backup:
            resource_bak_section += "[ResourceBakVB{0}]\n".format(vbuf_idx or 0)
            backup_section += "ResourceBakVB{0} = ref vb{0}\n".format(vbuf_idx or 0)
            restore_section += "vb{0} = ResourceBakVB{0}\n".format(vbuf_idx or 0)

    # FIXME: Maybe split this into several ini files that the user may or may
    # not choose to generate? One that just lists resources, a second that
    # lists the TextureOverrides to replace draw calls, and a third with the
    # ShaderOverride sections (or a ShaderRegex for foolproof replacements)...?
    f.write(
        textwrap.dedent("""
            ; Automatically generated file, be careful not to overwrite if you
            ; make any manual changes

            ; Please note - it is not recommended to place the [ShaderOverride]
            ; here, as you only want checktextureoverride executed once per
            ; draw call, so it's better to have all the shaders listed in a
            ; common file instead to avoid doubling up and to allow common code
            ; to enable/disable the mods, backup/restore buffers, etc. Plus you
            ; may need to locate additional shaders to take care of shadows or
            ; other render passes. But if you understand what you are doing and
            ; need a quick 'n' dirty way to enable the reinjection, fill this in
            ; and uncomment it:
            ;[ShaderOverride{suffix}]
            ;hash = FILL ME IN...
            ;checktextureoverride = vb0

            [TextureOverride{suffix}]
            ;hash = FILL ME IN...
            """)
        .lstrip()
        .format(
            suffix="",
        )
    )
    if ib is not None and "3DMigoto:FirstIndex" in obj:
        f.write("match_first_index = {}\n".format(obj["3DMigoto:FirstIndex"]))
    elif ib is None and "3DMigoto:FirstVertex" in obj:
        f.write("match_first_vertex = {}\n".format(obj["3DMigoto:FirstVertex"]))

    if backup:
        f.write(backup_section)

    f.write(bind_section)

    if topology == "trianglestrip":
        f.write("run = CustomShaderOverrideTopology\n")
    else:
        f.write(draw_section)

    if backup:
        f.write(restore_section)

    if topology == "trianglestrip":
        f.write(
            textwrap.dedent("""
            [CustomShaderOverrideTopology]
            topology = triangle_list
            """)
            + draw_section
        )

    if backup:
        f.write("\n" + resource_bak_section)

    f.write(resource_section)


def blender_vertex_to_3dmigoto_vertex(
    mesh: Mesh,
    obj: Object,
    blender_loop_vertex,
    layout,
    texcoords,
    blender_vertex,
    translate_normal,
    translate_tangent,
    export_outline=None,
):
    if blender_loop_vertex is not None:
        blender_vertex = mesh.vertices[blender_loop_vertex.vertex_index]
    vertex = {}
    blp_normal = list(blender_loop_vertex.normal)

    # TODO: Warn if vertex is in too many vertex groups for this layout,
    # ignoring groups with weight=0.0
    vertex_groups = sorted(blender_vertex.groups, key=lambda x: x.weight, reverse=True)

    for elem in layout:
        if elem.InputSlotClass != "per-vertex" or elem.reused_offset:
            continue

        semantic_translations = layout.get_semantic_remap()
        translated_elem_name, translated_elem_index = semantic_translations.get(
            elem.name, (elem.name, elem.SemanticIndex)
        )

        # Some games don't follow the official DirectX UPPERCASE semantic naming convention:
        translated_elem_name = translated_elem_name.upper()

        if translated_elem_name == "POSITION":
            if "POSITION.w" in custom_attributes_float(mesh):
                vertex[elem.name] = list(blender_vertex.undeformed_co) + [
                    custom_attributes_float(mesh)["POSITION.w"]
                    .data[blender_vertex.index]
                    .value
                ]
            else:
                vertex[elem.name] = elem.pad(list(blender_vertex.undeformed_co), 1.0)
        elif translated_elem_name.startswith("COLOR"):
            if elem.name in mesh.vertex_colors:
                vertex[elem.name] = elem.clip(
                    list(
                        mesh.vertex_colors[elem.name]
                        .data[blender_loop_vertex.index]
                        .color
                    )
                )
            else:
                vertex[elem.name] = list(
                    mesh.vertex_colors[elem.name + ".RGB"]
                    .data[blender_loop_vertex.index]
                    .color
                )[:3] + [
                    mesh.vertex_colors[elem.name + ".A"]
                    .data[blender_loop_vertex.index]
                    .color[0]
                ]
        elif translated_elem_name == "NORMAL":
            if "NORMAL.w" in custom_attributes_float(mesh):
                vertex[elem.name] = list(
                    map(translate_normal, blender_loop_vertex.normal)
                ) + [
                    custom_attributes_float(mesh)["NORMAL.w"]
                    .data[blender_vertex.index]
                    .value
                ]
            elif blender_loop_vertex:
                vertex[elem.name] = elem.pad(
                    list(map(translate_normal, blender_loop_vertex.normal)), 0.0
                )
            else:
                # XXX: point list topology, these normals are probably going to be pretty poor, but at least it's something to export
                vertex[elem.name] = elem.pad(
                    list(map(translate_normal, blender_vertex.normal)), 0.0
                )
        elif translated_elem_name.startswith("TANGENT"):
            if export_outline:
                # Genshin optimized outlines
                vertex[elem.name] = elem.pad(
                    list(
                        map(
                            translate_tangent,
                            export_outline.get(
                                blender_loop_vertex.vertex_index, blp_normal
                            ),
                        )
                    ),
                    blender_loop_vertex.bitangent_sign,
                )
            # DOAXVV has +1/-1 in the 4th component. Not positive what this is,
            # but guessing maybe the bitangent sign? Not even sure it is used...
            # FIXME: Other games
            elif blender_loop_vertex:
                vertex[elem.name] = elem.pad(
                    list(map(translate_tangent, blender_loop_vertex.tangent)),
                    blender_loop_vertex.bitangent_sign,
                )
            else:
                # XXX Blender doesn't save tangents outside of loops, so unless
                # we save these somewhere custom when importing they are
                # effectively lost. We could potentially calculate a tangent
                # from blender_vertex.normal, but there is probably little
                # point given that normal will also likely be garbage since it
                # wasn't imported from the mesh.
                pass
        elif translated_elem_name.startswith("BINORMAL"):
            # Some DOA6 meshes (skirts) use BINORMAL, but I'm not certain it is
            # actually the binormal. These meshes are weird though, since they
            # use 4 dimensional positions and normals, so they aren't something
            # we can really deal with at all. Therefore, the below is untested,
            # FIXME: So find a mesh where this is actually the binormal,
            # uncomment the below code and test.
            # normal = blender_loop_vertex.normal
            # tangent = blender_loop_vertex.tangent
            # binormal = numpy.cross(normal, tangent)
            # XXX: Does the binormal need to be normalised to a unit vector?
            # binormal = binormal / numpy.linalg.norm(binormal)
            # vertex[elem.name] = elem.pad(list(map(translate_binormal, binormal)), 0.0)
            pass
        elif translated_elem_name.startswith("BLENDINDICES"):
            i = translated_elem_index * 4
            vertex[elem.name] = elem.pad([x.group for x in vertex_groups[i : i + 4]], 0)
        elif translated_elem_name.startswith("BLENDWEIGHT"):
            # TODO: Warn if vertex is in too many vertex groups for this layout
            i = translated_elem_index * 4
            vertex[elem.name] = elem.pad(
                [x.weight for x in vertex_groups[i : i + 4]], 0.0
            )
        elif translated_elem_name.startswith("TEXCOORD") and elem.is_float():
            uvs = []
            for uv_name in ("%s.xy" % elem.remapped_name, "%s.zw" % elem.remapped_name):
                if uv_name in texcoords:
                    uvs += list(texcoords[uv_name][blender_loop_vertex.index])
            # Handle 1D + 3D TEXCOORDs. Order is important - 1D TEXCOORDs won't
            # match anything in above loop so only .x below, 3D TEXCOORDS will
            # have processed .xy part above, and .z part below
            for uv_name in ("%s.x" % elem.remapped_name, "%s.z" % elem.remapped_name):
                if uv_name in texcoords:
                    uvs += [texcoords[uv_name][blender_loop_vertex.index].x]
            vertex[elem.name] = uvs
        else:
            # Unhandled semantics are saved in vertex layers
            data = []
            for component in "xyzw":
                layer_name = "%s.%s" % (elem.name, component)
                if layer_name in custom_attributes_int(mesh):
                    data.append(
                        custom_attributes_int(mesh)[layer_name]
                        .data[blender_vertex.index]
                        .value
                    )
                elif layer_name in custom_attributes_float(mesh):
                    data.append(
                        custom_attributes_float(mesh)[layer_name]
                        .data[blender_vertex.index]
                        .value
                    )
            if data:
                # print('Retrieved unhandled semantic %s %s from vertex layer' % (elem.name, elem.Format), data)
                vertex[elem.name] = data

        if elem.name not in vertex:
            print("NOTICE: Unhandled vertex element: %s" % elem.name)
        # else:
        #    print('%s: %s' % (elem.name, repr(vertex[elem.name])))

    return vertex


def export_3dmigoto(
    operator: Operator, context: Context, vb_path, ib_path, fmt_path, ini_path
):
    obj = context.object
    if obj is None:
        raise Fatal("No object selected")

    strides = {
        x[11:-6]: obj[x]
        for x in obj.keys()
        if x.startswith("3DMigoto:VB") and x.endswith("Stride")
    }
    layout = InputLayout(obj["3DMigoto:VBLayout"])
    topology = "trianglelist"
    if "3DMigoto:Topology" in obj:
        topology = obj["3DMigoto:Topology"]
        if topology == "trianglestrip":
            operator.report(
                {"WARNING"},
                "trianglestrip topology not supported for export, and has been converted to trianglelist. Override draw call topology using a [CustomShader] section with topology=triangle_list",
            )
            topology = "trianglelist"
    if hasattr(context, "evaluated_depsgraph_get"):  # 2.80
        mesh = obj.evaluated_get(context.evaluated_depsgraph_get()).to_mesh()
    else:  # 2.79
        mesh = obj.to_mesh(context.scene, True, "PREVIEW", calc_tessface=False)
    mesh_triangulate(mesh)

    try:
        ib_format = obj["3DMigoto:IBFormat"]
    except KeyError:
        ib = None
    else:
        ib = IndexBuffer(ib_format)

    # Calculates tangents and makes loop normals valid (still with our
    # custom normal data from import time):
    try:
        mesh.calc_tangents()
    except RuntimeError as e:
        operator.report(
            {"WARNING"},
            "Tangent calculation failed, the exported mesh may have bad normals/tangents/lighting. Original {}".format(
                str(e)
            ),
        )

    texcoord_layers = {}
    for uv_layer in mesh.uv_layers:
        texcoords = {}

        try:
            flip_texcoord_v = obj["3DMigoto:" + uv_layer.name]["flip_v"]
            if flip_texcoord_v:
                flip_uv = lambda uv: (uv[0], 1.0 - uv[1])
            else:
                flip_uv = lambda uv: uv
        except KeyError:
            flip_uv = lambda uv: uv

        for loop in mesh.loops:
            uv = flip_uv(uv_layer.data[loop.index].uv)
            texcoords[loop.index] = uv
        texcoord_layers[uv_layer.name] = texcoords

    translate_normal = normal_export_translation(
        layout, Semantic.Normal, operator.flip_normal
    )
    translate_tangent = normal_export_translation(
        layout, Semantic.Tangent, operator.flip_tangent
    )

    # Blender's vertices have unique positions, but may have multiple
    # normals, tangents, UV coordinates, etc - these are stored in the
    # loops. To export back to DX we need these combined together such that
    # a vertex is a unique set of all attributes, but we don't want to
    # completely blow this out - we still want to reuse identical vertices
    # via the index buffer. There might be a convenience function in
    # Blender to do this, but it's easy enough to do this ourselves
    indexed_vertices = collections.OrderedDict()
    vb = VertexBufferGroup(layout=layout, topology=topology)
    vb.flag_invalid_semantics()
    if vb.topology == "trianglelist":
        for poly in mesh.polygons:
            face = []
            for blender_lvertex in mesh.loops[
                poly.loop_start : poly.loop_start + poly.loop_total
            ]:
                vertex = blender_vertex_to_3dmigoto_vertex(
                    mesh,
                    obj,
                    blender_lvertex,
                    layout,
                    texcoord_layers,
                    None,
                    translate_normal,
                    translate_tangent,
                )
                if ib is not None:
                    face.append(
                        indexed_vertices.setdefault(
                            HashableVertex(vertex), len(indexed_vertices)
                        )
                    )
                else:
                    if operator.flip_winding:
                        raise Fatal(
                            "Flipping winding order without index buffer not implemented"
                        )
                    vb.append(vertex)
            if ib is not None:
                if operator.flip_winding:
                    face.reverse()
                ib.append(face)

        if ib is not None:
            for vertex in indexed_vertices:
                vb.append(vertex)
    elif vb.topology == "pointlist":
        for index, blender_vertex in enumerate(mesh.vertices):
            vb.append(
                blender_vertex_to_3dmigoto_vertex(
                    mesh,
                    obj,
                    None,
                    layout,
                    texcoord_layers,
                    blender_vertex,
                    translate_normal,
                    translate_tangent,
                )
            )
            if ib is not None:
                ib.append((index,))
    else:
        raise Fatal('topology "%s" is not supported for export' % vb.topology)

    vgmaps = {
        k[15:]: keys_to_ints(v)
        for k, v in obj.items()
        if k.startswith("3DMigoto:VGMap:")
    }

    if "" not in vgmaps:
        vb.write(vb_path, strides, operator=operator)

    for suffix, vgmap in vgmaps.items():
        ib_path = vb_path
        if suffix:
            ib_path = f"{vb_path.parent / vb_path.stem}-{suffix}{vb_path.suffix}"
        vgmap_path = (ib_path.parent / ib_path.stem) + ".vgmap"
        print("Exporting %s..." % ib_path)
        vb.remap_blendindices(obj, vgmap)
        vb.write(ib_path, strides, operator=operator)
        vb.revert_blendindices_remap()
        sorted_vgmap = collections.OrderedDict(
            sorted(vgmap.items(), key=lambda x: x[1])
        )
        json.dump(sorted_vgmap, open(vgmap_path, "w"), indent=2)

    if ib is not None:
        ib.write(open(ib_path, "wb"), operator=operator)

    # Write format reference file
    write_fmt_file(open(fmt_path, "w"), vb, ib, strides)

    # Not ready yet
    # if ini_path:
    #    write_ini_file(open(ini_path, 'w'), vb, vb_path, ib, ib_path, strides, obj, orig_topology)


def register():
    """Register all classes"""
    bpy.types.Scene.xxmi = PointerProperty(type=XXMIProperties)


def unregister():
    """Unregister all classes"""
    del bpy.types.Scene.xxmi
