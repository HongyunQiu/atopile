import logging
from contextlib import contextmanager
from typing import Any, Dict, List, Literal, Optional

import attrs

from atopile.model.accessors import ModelVertexView
from atopile.model.model import EdgeType, Model, VertexType
from atopile.model.names import resolve_rel_name
from atopile.model.visitor import ModelVisitor

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)


@attrs.define
class Pin:
    # mandatory external
    name: str
    fields: Dict[str, Any]

    def to_dict(self) -> dict:
        return {"name": self.name, "fields": self.fields}


@attrs.define
class Link:
    # mandatory external
    name: str
    source: str
    target: str

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "source": self.source,
            "target": self.target,
        }


BlockType = Literal["file", "module", "component"]


@attrs.define
class Block:
    # mandatory external
    name: str
    type: str
    fields: Dict[str, Any]
    blocks: List["Block"]
    pins: List[Pin]
    links: List[Link]
    instance_of: Optional[str]

    # mandatory internal
    source: ModelVertexView

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "type": self.type,
            "fields": self.fields,
            "blocks": [block.to_dict() for block in self.blocks],
            "pins": [pin.to_dict() for pin in self.pins],
            "links": [link.to_dict() for link in self.links],
            "instance_of": self.instance_of,
        }


class Bob(ModelVisitor):
    """
    The builder... obviously
    """

    def __init__(self, model: Model) -> None:
        self.model = model
        self.all_verticies: List[ModelVertexView] = []
        self.block_stack: List[ModelVertexView] = []
        self.block_directory_by_path: Dict[str, Block] = {}
        self.pin_directory_by_vid: Dict[int, Pin] = {}
        super().__init__(model)

    @contextmanager
    def block_context(self, block: ModelVertexView):
        self.block_stack.append(block)
        yield
        self.block_stack.pop()

    @staticmethod
    def build(model: Model, main: ModelVertexView) -> Block:
        bob = Bob(model)

        root = bob.generic_visit_block(main)
        # TODO: this logic ultimately belongs in the viewer, because this
        # isn't really an instance of anything
        root.instance_of = main.path

        connections = model.graph.es.select(type_eq=EdgeType.connects_to.name)
        all_indicies = {v.index for v in bob.all_verticies}
        for connection in connections:
            if (
                connection.source not in all_indicies
                or connection.target not in all_indicies
            ):
                continue

            lca, link_name = resolve_rel_name(
                ModelVertexView.from_indicies(
                    model, [connection.source, connection.target]
                )
            )

            rel_source_mvvs = lca.relative_mvv_path(ModelVertexView(model, connection.source))
            rel_target_mvvs = lca.relative_mvv_path(ModelVertexView(model, connection.target))

            # FIXME: we need to screw with the paths of interface-owned pins
            # beacuse their pin names are indistinguisible from a sub-block's pins
            # we should ultimately fix this path thing when we rev. the viewer
            def _mod_path(mvv_path: list[ModelVertexView]) -> str:
                """
                Make relative paths, supplemeneting the '.'s after an interface with dashes
                eg. a.b.c -> a.b-c (where b is an interface)
                """
                pre_inf = []
                post_inf = []
                for i, mvv in enumerate(mvv_path):
                    if mvv.vertex_type == VertexType.interface:
                        pre_inf = mvv_path[:i]
                        post_inf = mvv_path[i:]
                        return ".".join(mvv.ref for mvv in pre_inf) + "." + "-".join(mvv.ref for mvv in post_inf)
                return ".".join(mvv.ref for mvv in mvv_path)

            link = Link(
                name=link_name,
                source=_mod_path(rel_source_mvvs),
                target=_mod_path(rel_target_mvvs),
            )

            bob.block_directory_by_path[lca.path].links.append(link)

        return root

    def generic_visit_block(self, vertex: ModelVertexView) -> Block:
        self.all_verticies.append(vertex)

        with self.block_context(vertex):
            # find subelements
            blocks: List[Block] = self.wander(
                vertex=vertex,
                mode="in",
                edge_type=EdgeType.part_of,
                vertex_type=[VertexType.component, VertexType.module],
            )

            pins = self.wander_interface(vertex)

            # check the type of this block
            instance_ofs = vertex.get_adjacents("out", EdgeType.instance_of)
            if len(instance_ofs) > 0:
                if len(instance_ofs) > 1:
                    log.warning(
                        f"Block {vertex.path} is an instance_of multiple things"
                    )
                instance_of = instance_ofs[0].path
            else:
                instance_of = None

            # do block build
            block = Block(
                name=vertex.ref,
                type=vertex.vertex_type.name,
                fields=vertex.data,  # FIXME: feels wrong to just blindly shove all this data down the pipe
                blocks=blocks,
                pins=pins,
                links=[],
                instance_of=instance_of,
                source=vertex,
            )

            self.block_directory_by_path[vertex.path] = block

        return block

    def visit_component(self, vertex: ModelVertexView) -> Block:
        return self.generic_visit_block(vertex)

    def visit_module(self, vertex: ModelVertexView) -> Block:
        return self.generic_visit_block(vertex)

    def wander_interface(self, vertex: ModelVertexView) -> List[Pin]:
        listy_pins: List[Pin, List[Pin]] = filter(
            lambda x: x is not None,
            self.wander(
                vertex=vertex,
                mode="in",
                edge_type=EdgeType.part_of,
                vertex_type=[VertexType.pin, VertexType.signal, VertexType.interface],
            ),
        )
        pins = []
        for listy_pin in listy_pins:
            if isinstance(listy_pin, list):
                pins += listy_pin
            else:
                pins += [listy_pin]
        return pins

    def visit_interface(self, vertex: ModelVertexView) -> List[Pin]:
        pins = self.wander_interface(vertex)
        for pin in pins:
            pin.name = vertex.ref + "-" + pin.name
        return pins

    def generic_visit_pin(self, vertex: ModelVertexView) -> Pin:
        vertex_data: dict = self.model.data.get(vertex.path, {})
        pin = Pin(name=vertex.ref, fields=vertex_data.get("fields", {}))
        self.pin_directory_by_vid[vertex.index] = pin
        return pin

    def visit_pin(self, vertex: ModelVertexView) -> Optional[Pin]:
        self.all_verticies.append(vertex)
        # filter out pins that have a single connection to a signal within the same block
        connections_in = vertex.get_edges(mode="in", edge_type=EdgeType.connects_to)
        connections_out = vertex.get_edges(mode="out", edge_type=EdgeType.connects_to)
        if len(connections_in) + len(connections_out) == 1:
            if len(connections_in) == 1:
                target = ModelVertexView(self.model, connections_in[0].source)
            if len(connections_out) == 1:
                target = ModelVertexView(self.model, connections_out[0].target)
            if target.vertex_type == VertexType.signal:
                if target.parent_path == vertex.parent_path:
                    return None

        return self.generic_visit_pin(vertex)

    def visit_signal(self, vertex: ModelVertexView) -> Pin:
        self.all_verticies.append(vertex)
        return self.generic_visit_pin(vertex)


# TODO: resolve the API between this and build_model
def build_view(model: Model, root_node: str) -> dict:
    root_node = ModelVertexView.from_path(model, root_node)
    root = Bob.build(model, root_node)
    return root.to_dict()