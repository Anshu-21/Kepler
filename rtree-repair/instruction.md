Our diagram editor stores every shape on the canvas in an R-tree. This is used for hover and marquee selection so that the editor does not have to scan every shape on the canvas. The current implementation in `/app/src/rtree.js` is broken. After enough inserts, deletes, and moves, it can return the wrong objects, lose or duplicate shapes, and leave old bounding boxes in the tree. This can also make queries visit completely unrelated branches.

Fix the R-tree. You can patch the existing implementation or replace it completely, but the public and structural contract below must stay the same. The editor and the graders both inspect the tree directly.

Only `/app/src/rtree.js` will be used for the final check. Anything else you create will be ignored. The code runs on the Node.js version already installed in the image, with no external packages.

The file must export the tree like this:

```js
module.exports = { RTree };
```

Shapes:

A shape is a plain object containing:

* `id`: a string
* `minX`, `minY`, `maxX`, `maxY`: finite numbers
* `minX <= maxX` and `minY <= maxY`
* optional numeric `z`, which defaults to `0`

Zero-width and zero-height shapes are valid.

IDs must be compared using normal JavaScript string ordering (`<`). Do not use locale-based sorting. For example, `"Zed"` comes before `"apple"` and `"a10"` comes before `"a2"`.

RTree API:

`new RTree({ maxEntries })` creates the tree.

* Default `maxEntries` is `9`.
* Minimum fill is `ceil(0.4 * maxEntries)`.

The tree must provide:

* `insert(shape)`
* `remove(id)`
* `update(id, bounds)`
* `queryRect(rect)`
* `hitTest(x, y)`
* `size` getter
* `resetStats()`

`insert` must throw if the ID already exists or the bounds are invalid.

`remove(id)` returns `true` when the shape existed and was removed, otherwise `false`.

`update(id, bounds)` changes the shape's bounds. It may also receive a new `z`; if `z` is not supplied, keep the old value. It must throw if the ID does not exist or the new bounds are invalid. If the operation throws, nothing in the tree may be changed.

`queryRect(rect)` returns fresh copies of all shapes whose boxes intersect the supplied rectangle. Results must be sorted by ID.

`hitTest(x, y)` returns a fresh copy of the shape containing the point with the highest `z`. If multiple shapes have the same `z`, the smaller ID wins. Return `null` when no shape contains the point.

Box rules:

All boxes use half-open ranges:

`[minX, maxX) × [minY, maxY)`

Two boxes intersect only when:

```text
a.minX < b.maxX
b.minX < a.maxX
a.minY < b.maxY
b.minY < a.maxY
```

So boxes that only touch along an edge do not intersect.

A point is inside a box when:

```text
minX <= x < maxX
minY <= y < maxY
```

A zero-area shape can therefore never be returned by `hitTest`.

Tree structure:

The internal structure is also part of the contract.

`tree.root` must be a node containing:

```js
{
  leaf: boolean,
  children: [...],
  bounds: {
    minX,
    minY,
    maxX,
    maxY
  },
  top: shape
}
```

An empty root has `bounds === null` and `top === null`.

For a leaf node, `children` contains the actual shape objects.

For an internal node, `children` contains other nodes.

All leaves must be at the same depth.

Every node can have at most `maxEntries` children. Every non-root node must have at least the minimum fill.

An internal root must have at least two children. A leaf root can contain anywhere from zero to `maxEntries` shapes.

The bounds stored on every node must always be the exact minimum bounding rectangle of its children. Do not leave larger or outdated bounds behind.

Every node's `top` is the shape that `hitTest` would choose if the point were inside every shape below that node: the highest `z`, and the smallest ID among shapes with that `z`. It must be exact at all times, including after a `z` change, a removal of the current top shape, a split or a reinsertion. In a real scene hundreds of large shapes can be stacked over the same point.

Each live shape must occur exactly once in the tree, using its current bounds and `z`.

Do not keep another complete copy of the scene and scan that instead. Query results must come from the R-tree itself.

Versions and undo:

The editor's undo stack keeps old versions of the scene, so the tree must be persistent.

`snapshot()` returns a read-only view of the tree as it is at that moment, in constant time. The view has `root`, `size`, `queryRect`, `hitTest`, `stats` and `resetStats`, behaving exactly like the same members of the tree, and its `root` obeys every structural rule above.

No later `insert`, `remove`, `update` or `restore` on the tree may change anything a snapshot reports, including node bounds and `top`. Nodes that an operation did not need to change must stay shared between the versions instead of being copied: a single `insert`, `remove` or `update` may create at most 40 new node objects that are not already part of the previous version.

`restore(view)` makes the tree equal to a snapshot taken from it earlier (it may take time proportional to the number of shapes). The tree can then be changed again, and neither that snapshot nor any other may be affected. The snapshot can be restored again later.

Statistics:

`tree.stats` must contain:

* `nodeVisits`
* `entryChecks`

`nodeVisits` increases whenever a node is entered while running `queryRect` or `hitTest`.

`entryChecks` increases whenever a shape is checked by a query.

`resetStats()` must set both values back to zero.

For a 50,000-shape scene, a query covering a small area should stay within:

```text
nodeVisits + entryChecks <= 400
```

This must still hold after tens of thousands of moves and deletions. In a 38,000-shape scene where several hundred large shapes cover any given point, `hitTest` must also stay within `nodeVisits + entryChecks <= 500`. The graders measure this directly from the tree, so simply reporting different numbers in `stats` will not help.

Cases to handle:

Do not assume the input is random. The tests include cases such as:

* many tiles sharing edges
* very tight clusters
* diagonal layouts
* a few large shapes containing thousands of smaller shapes
* shapes repeatedly moved between distant parts of the canvas
* deleting everything and then inserting again
* zero-area shapes
* snapshots taken after every single operation, then compared with the scene as it was, and undo to an old version followed by new edits
* many shapes with the same `z`, and `z` changes on shapes that are the current `top` of their ancestors

After every operation, the tree must give the same results as a brute-force scan of the current live shapes.

You have 9000 seconds to complete this task. Do not cheat by using online solutions or hints specific to this task.
