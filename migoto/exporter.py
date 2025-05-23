import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

import addon_utils
import bpy
import numpy
from bpy.types import Collection, Context, Depsgraph, Mesh, Object, Operator
from numpy.typing import NDArray

from .. import bl_info
from ..libs.jinja2 import Environment, FileSystemLoader
from .data.data_model import DataModelXXMI
from .data.byte_buffer import BufferLayout, Semantic, NumpyBuffer
from .datastructures import GameEnum
from .export_ops import mesh_triangulate
from .operators import Fatal


@dataclass
class SubObj:
    collection_name: str
    depth: int
    name: str
    obj: Object
    mesh: Mesh
    vertex_count: int = 0
    index_count: int = 0
    index_offset: int = 0


@dataclass
class TextureData:
    name: str
    extension: str
    hash: str


@dataclass
class Part:
    fullname: str
    objects: list[SubObj]
    textures: list[TextureData]
    first_index: int
    vertex_count: int = 0


@dataclass
class Component:
    fullname: str
    parts: list[Part]
    root_vs: str
    draw_vb: str
    position_vb: str
    blend_vb: str
    texcoord_vb: str
    ib: str
    vertex_count: int = 0
    strides: dict[str, int] = field(default_factory=dict)


@dataclass
class ModFile:
    name: str
    components: list[Component]
    hash_data: dict[str, str]
    game: GameEnum
    credit: str = ""


@dataclass
class ModExporter:
    # Input
    context: Context
    mod_name: str
    hash_data: list[dict]
    ignore_hidden: bool
    ignore_muted_shape_keys: bool
    apply_modifiers: bool
    only_selected: bool
    copy_textures: bool
    join_meshes: bool
    dump_path: Path
    destination: Path
    credit: str
    game: GameEnum
    operator: Operator
    outline_optimization: bool = False
    # Output
    mod_file: ModFile = field(init=False)
    ini_content: str = field(init=False)
    files_to_write: dict[Path, Union[str, NDArray]] = field(init=False)
    files_to_copy: dict[Path, Path] = field(init=False)

    def __post_init__(self) -> None:
        self.initialize_data()

    def initialize_data(self) -> None:
        print("Initializing data for export...")
        if not self.hash_data:
            raise ValueError("Hash data is empty!")

        candidate_objs: list[Object] = (
            [obj for obj in bpy.context.selected_objects]
            if self.only_selected
            else [obj for obj in self.context.scene.objects]
        )
        if self.ignore_hidden:
            candidate_objs = [obj for obj in candidate_objs if obj.visible_get()]
        if self.only_selected:
            selected_objs = [obj for obj in bpy.context.selected_objects]
            candidate_objs = [obj for obj in candidate_objs if obj in selected_objs]
        self.mod_file = ModFile(
            name=self.mod_name,
            components=[],
            hash_data=self.hash_data,
            game=self.game,
            credit=self.credit,
        )
        for i, component in enumerate(self.hash_data):
            current_name: str = f"{self.mod_name}{component['component_name']}"
            component_entry = Component(
                fullname=current_name,
                parts=[],
                root_vs=component["root_vs"],
                draw_vb=component["draw_vb"],
                position_vb=component["position_vb"],
                blend_vb=component["blend_vb"],
                texcoord_vb=component["texcoord_vb"],
                ib=component["ib"],
                strides={},
            )
            for j, part in enumerate(component["object_classifications"]):
                if component["draw_vb"] == "":
                    continue
                part_name: str = current_name + part
                matching_objs = [
                    obj for obj in candidate_objs if obj.name.startswith(part_name)
                ]
                if not matching_objs:
                    raise Fatal(f"Cannot find object {part_name} in the scene.")
                if len(matching_objs) > 1:
                    raise Fatal(f"Found multiple objects with the name {part_name}.")
                obj: Object = matching_objs[0]
                collection_name = [
                    c
                    for c in bpy.data.collections
                    if c.name.lower().startswith((part_name).lower())
                ]
                if len(collection_name) > 1:
                    raise Fatal(
                        f"ERROR: Found multiple collections with the name {part_name}. Ensure only one collection exists with that name."
                    )
                textures = []
                for entry in component["texture_hashes"][j]:
                    textures.append(TextureData(*entry))

                objects: list[SubObj] = []
                if len(collection_name) == 0:
                    self.obj_from_col(obj, None, objects)
                else:
                    self.obj_from_col(obj, collection_name[0], objects)
                offset = 0
                for entry in objects:
                    entry.index_count = len(entry.mesh.polygons) * 3
                    entry.index_offset = offset
                    offset += entry.index_count
                component_entry.parts.append(
                    Part(
                        fullname=part_name,
                        objects=objects,
                        textures=textures,
                        first_index=component["object_indexes"][j],
                    )
                )
            self.mod_file.components.append(component_entry)

    def obj_from_col(
        self,
        main_obj: Object,
        collection: Optional[Collection],
        destination: list[SubObj],
        depth: int = 0,
    ) -> None:
        """Recursively get all objects from a collection and its sub-collections."""
        depsgraph = bpy.context.evaluated_depsgraph_get()
        if destination == []:
            final_mesh: Mesh = self.process_mesh(main_obj, main_obj, depsgraph)
            destination.append(SubObj("", depth, main_obj.name, main_obj, final_mesh))
        if collection is None:
            return

        objs = [obj for obj in collection.objects if obj.type == "MESH"]
        if self.ignore_hidden:
            objs = [obj for obj in objs if obj.visible_get()]
        if self.only_selected:
            selected_objs = [obj for obj in bpy.context.selected_objects]
            objs = [obj for obj in objs if obj in selected_objs]
        sorted_objs = sorted(objs, key=lambda x: x.name)
        for obj in sorted_objs:
            final_mesh = self.process_mesh(main_obj, obj, depsgraph)
            destination.append(
                SubObj(collection.name, depth, obj.name, obj, final_mesh)
            )
        for child in collection.children:
            self.obj_from_col(main_obj, child, destination, depth + 1)

    def process_mesh(self, main_obj: Object, obj: Object, depsgraph: Depsgraph) -> Mesh:
        """Process the mesh of the object."""
        # TODO: Add moddifier application for SK'd meshes here
        final_mesh: Mesh = obj.evaluated_get(depsgraph).to_mesh()
        if main_obj != obj:
            # Matrix world seems to be the summatory of all transforms parents included
            # Might need to test for more edge cases and to confirm these suspicious,
            # other available options: matrix_local, matrix_basis, matrix_parent_inverse
            final_mesh.transform(obj.matrix_world)
            final_mesh.transform(main_obj.matrix_world.inverted())
        mesh_triangulate(final_mesh)
        return final_mesh

    def generate_buffers(self) -> None:
        """Generate buffers for the objects."""
        self.files_to_write = {}
        self.files_to_copy = {}
        repeated_textures = {}
        for component in self.mod_file.components:
            output_buffers: dict[str, NDArray] = {
                "Position": numpy.empty(0, dtype=numpy.uint8),
                "Blend": numpy.empty(0, dtype=numpy.uint8),
                "TexCoord": numpy.empty(0, dtype=numpy.uint8),
            }
            ib_offset: int = 0
            for part in component.parts:
                print(f"Processing {part.fullname} " + "-" * 10)
                ib_buffer = None
                data_model: DataModelXXMI = DataModelXXMI.from_obj(
                    part.objects[0].obj, self.game
                )
                for entry in part.objects:
                    print(f"Processing {entry.name}...")
                    v_count: int = 0
                    gen_buffers: dict[str, NumpyBuffer] = {
                        key: NumpyBuffer(layout=entry)
                        for key, entry in data_model.buffers_format.items()
                    }
                    if len(entry.obj.data.polygons) == 0:
                        continue
                    self.verify_mesh_requirements(
                        part.objects[0].obj,
                        entry.obj,
                        entry.mesh,
                        data_model.buffers_format,
                        [],
                    )
                    gen_buffers, v_count = data_model.get_data(
                        bpy.context,
                        None,
                        entry.obj,
                        entry.mesh,
                        [],
                        data_model.mirror_mesh,
                    )
                    for k in output_buffers:
                        if k not in gen_buffers:
                            continue
                        output_buffers[k] = (
                            gen_buffers[k].data
                            if len(output_buffers[k]) == 0
                            else numpy.concatenate(
                                (output_buffers[k], gen_buffers[k].data)  # type: ignore
                            )
                        )
                    gen_buffers["IB"].data["INDEX"] += ib_offset
                    ib_buffer = (
                        gen_buffers["IB"].data
                        if ib_buffer is None
                        else numpy.concatenate((ib_buffer, gen_buffers["IB"].data))
                    )
                    ib_offset += v_count
                    entry.vertex_count = v_count
                    part.vertex_count += v_count
                    component.vertex_count += v_count
                for t in part.textures:
                    if (
                        t.hash in repeated_textures
                        and self.game != GameEnum.GenshinImpact
                    ):
                        repeated_textures[t.hash].append(t)
                        continue
                    repeated_textures[t.hash] = [t]
                    tex_name = part.fullname + t.name + t.extension
                    self.files_to_copy[self.dump_path / tex_name] = (
                        self.destination / tex_name
                    )
                if ib_buffer is None:
                    print(f"Skipping {part.fullname}.ib due to no index data.")
                    continue
                self.files_to_write[self.destination / (part.fullname + ".ib")] = (
                    ib_buffer
                )
            self.optimize_outlines(output_buffers["Position"])
            if component.blend_vb != "":
                self.files_to_write[
                    self.destination / (component.fullname + "Position.buf")
                ] = output_buffers["Position"]
                self.files_to_write[
                    self.destination / (component.fullname + "Blend.buf")
                ] = output_buffers["Blend"]
                self.files_to_write[
                    self.destination / (component.fullname + "Texcoord.buf")
                ] = output_buffers["TexCoord"]
                for k, buffer in data_model.buffers_format.items():
                    if k == "IB":
                        continue
                    component.strides[k.lower()] = buffer.stride
                continue
            merged_dtype = numpy.dtype(
                list(output_buffers["Position"].dtype.descr)
                + list(output_buffers["TexCoord"].dtype.descr)
            )
            merged_buffer = numpy.empty(
                len(output_buffers["Position"]), dtype=merged_dtype
            )
            for key, entry in output_buffers.items():
                if key == "IB" or entry.dtype.names is None:
                    continue
                for name in entry.dtype.names:
                    merged_buffer[name] = entry[name]
            self.files_to_write[self.destination / (component.fullname + ".buf")] = (
                merged_buffer
            )
            component.strides = {"position": merged_buffer.itemsize}

    def verify_mesh_requirements(
        self,
        main_obj: Object,
        obj: Object,
        mesh: Mesh,
        buffers_format: dict[str, BufferLayout],
        excluded_buffers: list[str],
    ) -> None:
        """Checks for format requirements in specific layouts"""
        semantics_to_check = [
            semantic
            for key, buffer_layout in buffers_format.items()
            for semantic in buffer_layout.semantics
            if key not in excluded_buffers
        ]
        for sem in semantics_to_check:
            abs_enum = sem.abstract.enum
            abs_name = sem.abstract.get_name()
            if abs_enum == Semantic.Color and mesh.vertex_colors.get(abs_name) is None:
                raise Fatal(
                    f"Mesh({obj.name}) needs to have a color attribute called '{abs_name}'!"
                )
            if abs_enum == Semantic.TexCoord and mesh.uv_layers.get(abs_name) is None:
                raise Fatal(
                    f"Mesh({obj.name}) needs to have a uv attribute called '{abs_name}'!"
                )
            if abs_enum == Semantic.Blendweight:
                if len(mesh.vertices) > 0 and len(obj.vertex_groups) == 0:
                    self.operator.report(
                        {"WARNING"},
                        (
                            f"Mesh({obj.name}) requires vertex groups to be posed. "
                            "Please add vertex groups to the mesh if you intend for it to be rendered. "
                        ),
                    )
                max_groups = sem.format.get_num_values()
                for vertex in mesh.vertices:
                    if len(vertex.groups) > max_groups:
                        self.operator.report(
                            {"WARNING"},
                            (
                                f"Mesh({obj.name}) has some vertex with more VGs than the amount supported by the buffer format ({max_groups}). "
                                "Please remove the extra groups from the vertex or use to clean up the weights(limit total plus normalization). "
                            ),
                        )
                        break

    def generate_ini(
        self, template_name: str = "default.ini.j2", user_paths=None
    ) -> None:
        # Extensions handle modifiable paths differently. If we ever move to them we should make modifications in here
        print("Generating .ini file")
        addon_path: Path = Path(__file__).parent.parent
        for mod in addon_utils.modules():
            if mod.bl_info["name"] == "XXMI_Tools":
                addon_path = Path(mod.__file__).parent
                break
        templates_paths = [addon_path / "templates"]
        if user_paths is not None:
            templates_paths.extend(user_paths)
        env = Environment(
            loader=FileSystemLoader(searchpath=templates_paths),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        template = env.get_template(template_name)
        self.files_to_write[self.destination / (self.mod_name + ".ini")] = (
            template.render(
                version=bl_info["version"],
                mod_file=self.mod_file,
                credit=self.credit,
                game=self.game,
                character_name=self.mod_name,
                join_meshes=self.join_meshes,
            )
        )

    def optimize_outlines(self, position_buffer: NDArray) -> None:
        """Optimize the outlines of the meshes with angle-weighted normal averaging."""
        if not self.outline_optimization:
            return
        if len(position_buffer) == 0:
            return
        position = numpy.round(position_buffer["POSITION"], 3)
        u, u_inverse, counts = numpy.unique(
            position, axis=0, return_counts=True, return_inverse=True
        )
        position_buffer["TANGENT"][:, 0:3] = position_buffer["NORMAL"][:, 0:3]
        shared_indices = numpy.where(counts > 1)[0]
        for i in shared_indices:
            mask = u_inverse == i
            vertex_normals = position_buffer["NORMAL"][mask, 0:3]
            normal_magnitudes = numpy.linalg.norm(vertex_normals, axis=1)
            weights = normal_magnitudes / numpy.sum(normal_magnitudes)
            weighted_normals = vertex_normals * weights[:, numpy.newaxis]
            avg_normal = numpy.sum(weighted_normals, axis=0)
            if avg_normal[0] == 0 and avg_normal[1] == 0 and avg_normal[2] == 0:
                continue
            normalized_avg = avg_normal / numpy.linalg.norm(avg_normal)
            position_buffer["TANGENT"][mask, 0:3] = normalized_avg

    def write_files(self) -> None:
        """Write the files to the destination."""
        print("Writen files: ")
        self.destination.mkdir(parents=True, exist_ok=True)
        try:
            for file_path, content in self.files_to_write.items():
                print(f"{file_path.name}", end=", ")
                if isinstance(content, str):
                    with open(file_path, "w", encoding="utf-8") as file:
                        file.write(content)
                elif isinstance(content, numpy.ndarray):
                    content.tofile(file_path)
        except (OSError, IOError) as e:
            raise Fatal(f"Error writing file {file_path}: {e}")
        if not self.copy_textures:
            return
        try:
            for src, dest in self.files_to_copy.items():
                print(f"{dest.name}", end=", ")
                if not dest.exists():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(src, dest)
        except (OSError, IOError) as e:
            raise Fatal(f"Error copying file {src} to {dest}: {e}")
        print("")

    def export(self) -> None:
        """Export the mod file."""
        print(f"Exporting {self.mod_name} to {self.destination}")
        self.generate_buffers()
        self.generate_ini()
        self.write_files()

    def cleanup(self) -> None:
        """Cleanup the objects."""
        pass
