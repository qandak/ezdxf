# Source package: "py3dbp" hosted on PyPI
# (c) Enzo Ruiz Pelaez
# https://github.com/enzoruiz/3dbinpacking
# License: MIT License
# Credits:
# - https://github.com/enzoruiz/3dbinpacking/blob/master/erick_dube_507-034.pdf
# - https://github.com/gedex/bp3d - implementation in Go
# - https://github.com/bom-d-van/binpacking - implementation in Go
#
# ezdxf add-on:
# License: MIT License
# (c) 2022, Manfred Moitzi:
# - refactoring
# - type annotations
# - adaptations:
#   - removing Decimal class usage
#   - utilizing ezdxf.math.BoundingBox for intersection checks
#   - removed non-distributing mode; copy packer and use different bins for each copy
# - additions:
#   - Item.get_transformation()
#   - shuffle_pack()
#   - AbstractPacker.schematic_pack() interface for genetic algorithms
#   - DXF exporter for debugging

from typing import (
    Tuple,
    List,
    Iterable,
    TYPE_CHECKING,
    Iterator,
    TypeVar,
    Sequence,
    Dict,
    Optional,
    Callable,
)
import abc
import array
from enum import Enum, auto
import copy
import itertools
import math
import random
import time


from ezdxf.enums import TextEntityAlignment

from ezdxf.math import (
    Vec2,
    Vec3,
    Vertex,
    BoundingBox,
    BoundingBox2d,
    AbstractBoundingBox,
    Matrix44,
)

if TYPE_CHECKING:
    from ezdxf.eztypes import GenericLayoutType

__all__ = [
    "Item",
    "FlatItem",
    "Box",  # contains Item
    "Envelope",  # contains FlatItem
    "AbstractPacker",
    "Packer",
    "FlatPacker",
    "RotationType",
    "PickStrategy",
    "shuffle_pack",
    "export_dxf",
]

UNLIMITED_WEIGHT = 1e99
T = TypeVar("T")


class RotationType(Enum):
    WHD = auto()
    HWD = auto()
    HDW = auto()
    DHW = auto()
    DWH = auto()
    WDH = auto()


class Axis(Enum):
    WIDTH = auto()
    HEIGHT = auto()
    DEPTH = auto()


class PickStrategy(Enum):
    SMALLER_FIRST = auto()
    BIGGER_FIRST = auto()
    SHUFFLE = auto()


START_POSITION: Tuple[float, float, float] = (0, 0, 0)


class Item:
    def __init__(
        self,
        payload,
        width: float,
        height: float,
        depth: float,
        weight: float = 0.0,
    ):
        self.payload = payload  # arbitrary associated Python object
        self.width = float(width)
        self.height = float(height)
        self.depth = float(depth)
        self.weight = float(weight)
        self._rotation_type = RotationType.WHD
        self._position = START_POSITION
        self._bbox: AbstractBoundingBox = BoundingBox()
        self._tainted_bbox = True

    def copy(self):
        # All copies have a reference to the same payload
        return copy.copy(self)  # shallow copy

    def get_volume(self):
        return self.width * self.height * self.depth

    def _update_bbox(self) -> None:
        v1 = Vec3(self._position)
        self._bbox = BoundingBox([v1, v1 + Vec3(self.get_dimension())])

    def __str__(self):
        return (
            f"{str(self.payload)}({self.width}x{self.height}x{self.depth}, "
            f"weight: {self.weight}) pos({str(self.position)}) "
            f"rt({self.rotation_type}) vol({self.get_volume()})"
        )

    @property
    def bbox(self) -> AbstractBoundingBox:
        if self._tainted_bbox:
            self._update_bbox()
            self._tainted_bbox = False
        return self._bbox

    @property
    def rotation_type(self) -> RotationType:
        return self._rotation_type

    @rotation_type.setter
    def rotation_type(self, value: RotationType) -> None:
        self._rotation_type = value
        self._tainted_bbox = True

    @property
    def position(self) -> Tuple[float, float, float]:
        return self._position

    @position.setter
    def position(self, value: Tuple[float, float, float]) -> None:
        self._position = value
        self._tainted_bbox = True

    def get_dimension(self) -> Tuple[float, float, float]:
        rt = self.rotation_type
        if rt == RotationType.WHD:
            return self.width, self.height, self.depth
        elif rt == RotationType.HWD:
            return self.height, self.width, self.depth
        elif rt == RotationType.HDW:
            return self.height, self.depth, self.width
        elif rt == RotationType.DHW:
            return self.depth, self.height, self.width
        elif rt == RotationType.DWH:
            return self.depth, self.width, self.height
        elif rt == RotationType.WDH:
            return self.width, self.depth, self.height
        raise ValueError(rt)

    def get_transformation(self) -> Matrix44:
        """Returns the transformation matrix to transform the source entity
        located with the minimum extension corner of its bounding box in
        (0, 0, 0) to the final location including the required rotation.
        """
        x, y, z = self.position
        rt = self.rotation_type
        if rt == RotationType.WHD:
            return Matrix44.translate(x, y, z)
        elif rt == RotationType.HWD:
            # height, width, depth orientation
            return Matrix44.z_rotate(math.pi / 2) @ Matrix44.translate(
                x + self.height, y, 0
            )
        raise NotImplementedError(f"rotation {str(rt)} not supported yet")


class FlatItem(Item):
    def __init__(
        self,
        payload,
        width: float,
        height: float,
        weight: float = 0.0,
    ):
        super().__init__(payload, width, height, 1.0, weight)

    def _update_bbox(self) -> None:
        v1 = Vec2(self._position)
        self._bbox = BoundingBox2d([v1, v1 + Vec2(self.get_dimension())])

    def __str__(self):
        return (
            f"{str(self.payload)}({self.width}x{self.height}, "
            f"weight: {self.weight}) pos({str(self.position)}) "
            f"rt({self.rotation_type}) area({self.get_volume()})"
        )


class Bin:
    def __init__(
        self,
        name,
        width: float,
        height: float,
        depth: float,
        max_weight: float = UNLIMITED_WEIGHT,
    ):
        self.name = name
        self.width = float(width)
        self.height = float(height)
        self.depth = float(depth)
        self.max_weight = float(max_weight)
        self.items: List[Item] = []

    def copy(self):
        box = copy.copy(self)  # shallow copy
        box.items = list(self.items)
        return box

    def reset(self):
        self.items.clear()

    @property
    def is_empty(self) -> bool:
        return not len(self.items)

    def __str__(self) -> str:
        return (
            f"{str(self.name)}({self.width:.3f}x{self.height:.3f}x{self.depth:.3f}, "
            f"max_weight:{self.max_weight}) "
            f"vol({self.get_capacity():.3f})"
        )

    def put_item(self, item: Item, pivot: Tuple[float, float, float]) -> bool:
        valid_item_position = item.position
        item.position = pivot
        x, y, z = pivot

        # Try all possible rotations:
        for rotation_type in self.rotations():
            item.rotation_type = rotation_type
            w, h, d = item.get_dimension()
            if self.width < x + w or self.height < y + h or self.depth < z + d:
                continue
            # new item fits inside the box at he current location and rotation:
            item_bbox = item.bbox
            if (
                not any(item_bbox.intersect(i.bbox) for i in self.items)
                and self.get_total_weight() + item.weight <= self.max_weight
            ):
                self.items.append(item)
                return True

        item.position = valid_item_position
        return False

    def get_capacity(self) -> float:
        """Returns the maximum fill volume of the bin."""
        return self.width * self.height * self.depth

    def get_total_weight(self) -> float:
        """Returns the total weight of all fitted items."""
        return sum(item.weight for item in self.items)

    def get_total_volume(self) -> float:
        """Returns the total volume of all fitted items."""
        return sum(item.get_volume() for item in self.items)

    def get_fill_ratio(self) -> float:
        """Return the fill ratio."""
        return self.get_total_volume() / self.get_capacity()

    def rotations(self) -> Iterable[RotationType]:
        return RotationType


class Box(Bin):
    pass


class Envelope(Bin):
    def __init__(
        self,
        name,
        width: float,
        height: float,
        max_weight: float = UNLIMITED_WEIGHT,
    ):
        super().__init__(name, width, height, 1.0, max_weight)

    def __str__(self) -> str:
        return (
            f"{str(self.name)}({self.width:.3f}x{self.height:.3f}, "
            f"max_weight:{self.max_weight}) "
            f"area({self.get_capacity():.3f})"
        )

    def rotations(self) -> Iterable[RotationType]:
        return RotationType.WHD, RotationType.HWD


def _smaller_first(bins: List, items: List) -> None:
    # SMALLER_FIRST is often very bad! Especially for many in small
    # amounts increasing sizes.
    bins.sort(key=lambda b: b.get_capacity())
    items.sort(key=lambda i: i.get_volume())


def _bigger_first(bins: List, items: List) -> None:
    # BIGGER_FIRST is the best strategy
    bins.sort(key=lambda b: b.get_capacity(), reverse=True)
    items.sort(key=lambda i: i.get_volume(), reverse=True)


def _shuffle(bins: List, items: List) -> None:
    # Better as SMALLER_FIRST
    random.shuffle(bins)
    random.shuffle(items)


PICK_STRATEGY = {
    PickStrategy.SMALLER_FIRST: _smaller_first,
    PickStrategy.BIGGER_FIRST: _bigger_first,
    PickStrategy.SHUFFLE: _shuffle,
}


class AbstractPacker(abc.ABC):
    def __init__(self):
        self.bins: List[Bin] = []
        self.items: List[Item] = []
        self._init_state = True

    def copy(self):
        """Copy packer in init state to apply different pack strategies."""
        if self.is_packed:
            raise TypeError("cannot copy packed state")
        if not all(box.is_empty for box in self.bins):
            raise TypeError("bins contain data in unpacked state")
        packer = self.__class__()
        packer.bins = [box.copy() for box in self.bins]
        packer.items = [item.copy() for item in self.items]
        return packer

    @property
    def is_packed(self) -> bool:
        return not self._init_state

    @property
    def unfitted_items(self) -> List[Item]:  # just an alias
        return self.items

    def __str__(self) -> str:
        fill = ""
        if self.is_packed:
            fill = f", fill ratio: {self.get_fill_ratio()}"
        return f"{self.__class__.__name__}, {len(self.bins)} bins{fill}"

    def append_bin(self, box: Bin) -> None:
        if self.is_packed:
            raise TypeError("cannot append bins to packed state")
        if not box.is_empty:
            raise TypeError("cannot append bins with content")
        self.bins.append(box)

    def append_item(self, item: Item) -> None:
        if self.is_packed:
            raise TypeError("cannot append items to packed state")
        self.items.append(item)

    def get_fill_ratio(self) -> float:
        """Return the fill ratio of all bins."""
        total_capacity = self.get_capacity()
        if total_capacity == 0.0:
            return 0.0
        return self.get_total_volume() / total_capacity

    def get_capacity(self) -> float:
        """Returns the maximum fill volume of all bins."""
        return sum(box.get_capacity() for box in self.bins)

    def get_total_weight(self) -> float:
        """Returns the total weight of all fitted items in all bins."""
        return sum(box.get_total_weight() for box in self.bins)

    def get_total_volume(self) -> float:
        """Returns the total volume of all fitted items in all bins."""
        return sum(box.get_total_volume() for box in self.bins)

    def pack(self, pick=PickStrategy.BIGGER_FIRST) -> None:
        """Pack items into bins. Distributes all items across all bins."""
        PICK_STRATEGY[pick](self.bins, self.items)
        # items are removed from self.items while packing!
        self._pack(self.bins, list(self.items))
        # unfitted items remain in self.items

    def _pack(self, bins: Iterable[Bin], items: Iterable[Item]) -> None:
        """Pack items into bins, removes packed items from self.items!"""
        self._init_state = False
        for box in bins:
            for item in items:
                if self.pack_to_bin(box, item):
                    self.items.remove(item)
        # unfitted items remain in self.items

    def schematic_pack(
        self, item_schema: Iterator[float], bin_schema: Iterator[float] = None
    ) -> None:
        # fixed ascending base order
        _smaller_first(self.bins, self.items)
        if bin_schema is None:
            bin_schema = itertools.repeat(1.0)  # bigger first
        self._pack(
            # schematic picker uses a shallow copy of the input data!
            schematic_picker(self.bins, bin_schema),
            schematic_picker(self.items, item_schema),
        )
        # unfitted items remain in self.items

    @staticmethod
    @abc.abstractmethod
    def pack_to_bin(box: Bin, item: Item) -> bool:
        ...


def shuffle_pack(packer: AbstractPacker, attempts: int) -> AbstractPacker:
    """Random shuffle packing. Returns a new packer with the best packing result,
    the input packer is unchanged.
    """
    if attempts < 1:
        raise ValueError("expected attempts >= 1")
    best_ratio = 0.0
    best_packer = packer
    for _ in range(attempts):
        new_packer = packer.copy()
        new_packer.pack(PickStrategy.SHUFFLE)
        new_ratio = new_packer.get_fill_ratio()
        if new_ratio > best_ratio:
            best_ratio = new_ratio
            best_packer = new_packer
    return best_packer


def schematic_picker(
    items: Iterable[T], schema: Iterator[float]
) -> Iterator[T]:
    """Yields all `items` in the order defined by the pick schema.
    The pick values have to be in the range [0, 1] and determine the
    location from where to pick the next item. E.g. 0 picks from the front, 1
    picks from the end and 0.5 picks from the middle. For each item is a pick
    value from the `schema` required!

    Args:
        items: iterable of input data
        schema: iterator of pick values as float in range [0, 1]

    Raises:
        ValueError: invalid pick value or not enough pick values

    """
    items = list(items)
    while len(items):
        try:
            value = next(schema)
        except StopIteration:
            raise ValueError("not enough pick values")
        try:
            yield items.pop(round(abs(value) * (len(items) - 1)))
        except IndexError:
            raise ValueError("pick values have to be in range [0, 1]")


class Packer(AbstractPacker):
    """3D Packer."""

    def add_bin(
        self,
        name: str,
        width: float,
        height: float,
        depth: float,
        max_weight: float = UNLIMITED_WEIGHT,
    ) -> Box:
        box = Box(name, width, height, depth, max_weight)
        self.append_bin(box)
        return box

    def add_item(
        self,
        payload,
        width: float,
        height: float,
        depth: float,
        weight: float = 0.0,
    ) -> Item:
        item = Item(payload, width, height, depth, weight)
        self.append_item(item)
        return item

    @staticmethod
    def pack_to_bin(box: Bin, item: Item) -> bool:
        if not box.items:
            return box.put_item(item, START_POSITION)

        for axis in Axis:
            for placed_item in box.items:
                w, h, d = placed_item.get_dimension()
                x, y, z = placed_item.position
                if axis == Axis.WIDTH:
                    pivot = (x + w, y, z)  # new item right of the placed item
                elif axis == Axis.HEIGHT:
                    pivot = (x, y + h, z)  # new item above of the placed item
                elif axis == Axis.DEPTH:
                    pivot = (x, y, z + d)  # new item on top of the placed item
                else:
                    raise TypeError(axis)
                if box.put_item(item, pivot):
                    return True

        return False


class FlatPacker(AbstractPacker):
    """2D Packer."""

    def add_bin(
        self,
        name: str,
        width: float,
        height: float,
        max_weight: float = UNLIMITED_WEIGHT,
    ) -> Envelope:
        envelope = Envelope(name, width, height, max_weight)
        self.append_bin(envelope)
        return envelope

    def add_item(
        self,
        payload,
        width: float,
        height: float,
        weight: float = 0.0,
    ) -> Item:
        item = FlatItem(payload, width, height, weight)
        self.append_item(item)
        return item

    @staticmethod
    def pack_to_bin(envelope: Bin, item: Item) -> bool:
        if not envelope.items:
            return envelope.put_item(item, START_POSITION)

        for axis in (Axis.WIDTH, Axis.HEIGHT):
            for ib in envelope.items:
                w, h, _ = ib.get_dimension()
                x, y, _ = ib.position
                if axis == Axis.WIDTH:
                    pivot = (x + w, y, 0)
                elif axis == Axis.HEIGHT:
                    pivot = (x, y + h, 0)
                else:
                    raise TypeError(axis)
                if envelope.put_item(item, pivot):
                    return True
        return False


def _to_list(values) -> List[float]:
    return array.array("f", values)  # type: ignore


class Gene:
    __slots__ = ("_length", "_data", "fitness")

    def __init__(self, length: int, value: float = 0.0):
        self._length = int(length)
        if 0.0 <= value <= 1.0:
            self._data: List[float] = _to_list(
                itertools.repeat(value, self._length)
            )
        else:
            raise ValueError("data value out of range")
        self.fitness: Optional[float] = None

    @classmethod
    def random(cls, length: int) -> "Gene":
        g = cls(length)
        g.reset([random.random() for _ in range(length)])
        return g

    def _check_valid_data(self):
        if len(self._data) != self._length:
            raise ValueError("invalid data count")
        if not all(0.0 <= v <= 1.0 for v in self._data):
            raise ValueError("data value out of range")

    def copy(self):
        return copy.deepcopy(self)

    def taint(self):
        self.fitness = None

    def __eq__(self, other):
        assert isinstance(other, Gene)
        return self._data == other._data

    def __str__(self):
        fitness = ", fitness=None"
        if fitness is None:
            fitness = ", fitness=None"
        else:
            fitness = f", fitness={self.fitness:.4f}"
        return f"{str([round(v, 4) for v in self._data])}{fitness}"

    def __len__(self):
        return len(self._data)

    def __getitem__(self, item):
        return self._data.__getitem__(item)

    def __iter__(self):
        return iter(self._data)

    def reset(self, values: Iterable[float]):
        self._data = _to_list(values)
        self._check_valid_data()
        self.taint()

    def mutate(self, rate: float):
        for index in range(self._length):
            if random.random() < rate:
                self.mutate_at(index)

    def mutate_at(self, index):
        self._data[index] = 1.0 - self._data[index]  # flip pick location
        self.taint()

    def replace_back(self, part: Sequence) -> None:
        self._data[-len(part) :] = _to_list(part)
        self._check_valid_data()
        self.taint()

    def replace_front(self, part: Sequence) -> None:
        self._data[: len(part)] = _to_list(part)
        self._check_valid_data()
        self.taint()


def recombine_genes(gene1: Gene, gene2: Gene, index: int) -> None:
    part1 = gene1[index:]
    part2 = gene2[index:]
    gene1.replace_back(part2)
    gene2.replace_back(part1)


class GeneticSolver:
    def __init__(
        self,
        packer: AbstractPacker,
        max_runs: int,
        max_fitness: float,
        crossover_rate: float = 0.70,
        mutation_rate: float = 0.001,
    ):
        if max_fitness > 1.0 or max_fitness < 0.0:
            raise ValueError("max_fitness not in range [0, 1]")
        self._max_fitness = float(max_fitness)
        if max_runs < 1:
            raise ValueError("max_runs < 1")
        self._max_runs = int(max_runs)
        if packer.is_packed:
            raise ValueError("packer is already packed")
        self._packer = packer
        self._required_gene_length = len(packer.items)
        self._genes: List[Gene] = []
        self._crossover_rate = float(crossover_rate)
        self._mutation_rate = float(mutation_rate)
        self.best_fitness: float = 0.0
        self.best_gene = Gene(0)
        self.best_packer = packer
        self.run: int = 0

    @property
    def is_executed(self) -> bool:
        return bool(self.run)

    def add_gene(self, gene: Gene):
        if not self.is_executed:
            if len(gene) != self._required_gene_length:
                raise ValueError(
                    f"invalid gene length, requires {self._required_gene_length}"
                )
            self._genes.append(gene)
        else:
            raise TypeError("already executed")

    def add_random_genes(self, count: int):
        for _ in range(count):
            self.add_gene(Gene.random(self._required_gene_length))

    def execute(
        self,
        feedback: Callable = None,
        interval: float = 1.0,
        max_time: float = 1e99,
    ) -> None:
        if self.is_executed:
            raise TypeError("can only run once")
        t0 = time.perf_counter()
        start_time = t0
        for run in range(self._max_runs):
            self.run = run
            self._measure_fitness()
            if self.best_fitness >= self._max_fitness:
                break
            t1 = time.perf_counter()
            if start_time - t1 > max_time:
                break
            if feedback and t1 - t0 > interval:
                feedback()
                t0 = t1
            self._selection()

    def _measure_fitness(self):
        for gene in self._genes:
            if gene.fitness is not None:
                continue
            p0 = self._packer.copy()
            p0.schematic_pack(iter(gene))
            fill_ratio = p0.get_fill_ratio()
            gene.fitness = fill_ratio
            if fill_ratio > self.best_fitness:
                self.best_fitness = fill_ratio
                self.best_packer = p0
                self.best_gene = gene

    def _selection(self):
        wheel = self._make_wheel()
        genes: List[Gene] = []
        count = len(self._genes)
        while len(genes) < count:
            gene1, gene2 = wheel.pick(2)
            gene1 = gene1.copy()
            gene2 = gene2.copy()
            if random.random() < self._crossover_rate:
                location = random.randrange(0, len(gene1))
                recombine_genes(gene1, gene2, location)
            gene1.mutate(self._mutation_rate)
            gene2.mutate(self._mutation_rate)
            genes.append(gene1)
            genes.append(gene2)
        self._genes = genes

    def _make_wheel(self):
        wheel = WheelOfFortune()
        genes = self._genes
        sum_fitness = sum(g.fitness for g in genes)
        if sum_fitness == 0.0:
            sum_fitness = 1.0
        for gene in genes:
            wheel.add_gene(gene, gene.fitness / sum_fitness)
        return wheel


class WheelOfFortune:
    def __init__(self):
        self._items: List[Gene] = []
        self._weights: List[float] = []

    def add_gene(self, item: Gene, weight: float):
        self._items.append(item)
        self._weights.append(weight)

    def pick(self, count: int) -> Iterable[Gene]:
        return random.choices(self._items, self._weights, k=count)


def export_dxf(
    layout: "GenericLayoutType", bins: List[Bin], offset: Vertex = (1, 0, 0)
) -> None:
    from ezdxf import colors

    offset_vec = Vec3(offset)
    start = Vec3()
    index = 0
    rgb = (colors.RED, colors.GREEN, colors.BLUE, colors.MAGENTA, colors.CYAN)
    for box in bins:
        m = Matrix44.translate(start.x, start.y, start.z)
        _add_frame(layout, box, "FRAME", m)
        for item in box.items:
            _add_mesh(layout, item, "ITEMS", rgb[index], m)
            index += 1
            if index >= len(rgb):
                index = 0
        start += offset_vec


def _add_frame(layout: "GenericLayoutType", box: Bin, layer: str, m: Matrix44):
    def add_line(v1, v2):
        line = layout.add_line(v1, v2, dxfattribs=attribs)
        line.transform(m)

    attribs = {"layer": layer}
    x0, y0, z0 = (0.0, 0.0, 0.0)
    x1 = float(box.width)
    y1 = float(box.height)
    z1 = float(box.depth)
    corners = [
        (x0, y0),
        (x1, y0),
        (x1, y1),
        (x0, y1),
        (x0, y0),
    ]
    for (sx, sy), (ex, ey) in zip(corners, corners[1:]):
        add_line((sx, sy, z0), (ex, ey, z0))
        add_line((sx, sy, z1), (ex, ey, z1))
    for x, y in corners[:-1]:
        add_line((x, y, z0), (x, y, z1))

    text = layout.add_text(box.name, height=0.25, dxfattribs=attribs)
    text.set_placement((x0 + 0.25, y1 - 0.5, z1))
    text.transform(m)


def _add_mesh(
    layout: "GenericLayoutType", item: Item, layer: str, color: int, m: Matrix44
):
    from ezdxf.render.forms import cube

    attribs = {
        "layer": layer,
        "color": color,
    }
    mesh = cube(center=False)
    sx, sy, sz = item.get_dimension()
    mesh.scale(sx, sy, sz)
    x, y, z = item.position
    mesh.translate(x, y, z)
    mesh.render_polyface(layout, attribs, matrix=m)
    text = layout.add_text(
        str(item.payload), height=0.25, dxfattribs={"layer": "TEXT"}
    )
    if sy > sx:
        text.dxf.rotation = 90
        align = TextEntityAlignment.TOP_LEFT
    else:
        align = TextEntityAlignment.BOTTOM_LEFT
    text.set_placement((x + 0.25, y + 0.25, z + sz), align=align)
    text.transform(m)
