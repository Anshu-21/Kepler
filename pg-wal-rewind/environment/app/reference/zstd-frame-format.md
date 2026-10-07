# Zstandard frame format (decoder's view)

With `wal_compression = zstd`, PostgreSQL compresses a page image (the page without its hole) with `ZSTD_compress()` at the default level. The stored bytes are one Zstandard frame, as specified in RFC 8878. This note covers what a decoder needs for such frames. All multi-byte integers are little-endian.

## Frame

| field | size | content |
| --- | --- | --- |
| magic | 4 | `0xFD2FB528` |
| frame header descriptor | 1 | bits 7-6 `FCS_flag`, bit 5 `Single_Segment`, bit 2 `Checksum_flag`, bits 1-0 `DictID_flag` |
| window descriptor | 0 or 1 | present only when `Single_Segment` is 0 |
| dictionary id | 0, 1, 2 or 4 | size given by `DictID_flag` = 0, 1, 2, 3 |
| frame content size | 0, 1, 2, 4 or 8 | `FCS_flag` 0 gives 1 byte if `Single_Segment`, else none; 1 gives 2 bytes (value + 256); 2 gives 4; 3 gives 8 |
| blocks | | one or more |
| checksum | 0 or 4 | present when `Checksum_flag` is 1 (low 32 bits of XXH64; may be ignored) |

Each block starts with a 3-byte header: bit 0 `Last_Block`, bits 2-1 `Block_Type`, bits 23-3 `Block_Size`.

* Type 0, raw: `Block_Size` bytes copied to the output.
* Type 1, RLE: one byte, repeated `Block_Size` times.
* Type 2, compressed: `Block_Size` bytes holding a literals section and then a sequences section.
* Type 3 is reserved.

Decoding state carried from block to block within a frame: the last Huffman table, the last three FSE tables (literal lengths, offsets, match lengths), and the three repeat offsets, which start as 1, 4, 8.

## Literals section

The low 2 bits of the first byte give `Literals_Block_Type`: 0 raw, 1 RLE, 2 compressed, 3 treeless (compressed with the previous Huffman table). Bits 3-2 give `Size_Format`.

Raw and RLE literals:

* `Size_Format` 0 or 2: a 1-byte header, and the size is `byte0 >> 3`.
* 1: a 2-byte header, and the size is `(byte0 >> 4) + (byte1 << 4)`.
* 3: a 3-byte header, and the size is `(byte0 >> 4) + (byte1 << 4) + (byte2 << 12)`.

Raw literals are that many bytes. RLE literals are one byte repeated that many times.

Compressed and treeless literals: read the header as one little-endian number `h`.

| `Size_Format` | streams | header bytes | regenerated size | compressed size |
| --- | --- | --- | --- | --- |
| 0 | 1 | 3 | `(h >> 4) & 0x3FF` | `(h >> 14) & 0x3FF` |
| 1 | 4 | 3 | `(h >> 4) & 0x3FF` | `(h >> 14) & 0x3FF` |
| 2 | 4 | 4 | `(h >> 4) & 0x3FFF` | `(h >> 18) & 0x3FFF` |
| 3 | 4 | 5 | `(h >> 4) & 0x3FFFF` | `(h >> 22) & 0x3FFFF` |

The compressed size counts everything after the header: the Huffman tree description (type 2 only), the jump table and the streams.

With four streams, a 6-byte jump table comes first. It holds the sizes of streams 1 to 3 as three 2-byte values, and stream 4 takes the rest. Streams 1 to 3 each decode `(regenerated + 3) / 4` literals, and stream 4 decodes the remainder.

### Huffman tree description

A header byte `hb` comes first.

* `hb >= 128`: the weights are stored directly, 4 bits each, for `hb - 127` symbols, two per byte with the first weight in the high nibble. They take `ceil((hb - 127) / 2)` bytes.
* `hb < 128`: the next `hb` bytes are FSE-compressed weights. They start with an FSE table description (maximum accuracy log 6), followed by a backward bitstream decoded with two interleaved states (see FSE below).

Weights are given for symbols 0, 1, 2, and so on. The last symbol's weight is implied:

1. Let `total` be the sum of `2^(w-1)` over the given non-zero weights.
2. `Max_Bits` is the bit length of `total`.
3. The last weight is `log2(2^Max_Bits - total) + 1`. The difference `2^Max_Bits - total` is always a power of two.

A symbol of weight `w > 0` has a code `Max_Bits + 1 - w` bits long. Weight 0 means the symbol is unused.

Decoding table: it has `2^Max_Bits` entries. Go through the weights from 1 upward, and for each weight through the symbols in increasing order. Give each such symbol the next `2^(w-1)` consecutive entries, starting at entry 0. An entry holds the symbol and its code length.

To decode one literal:

1. Peek `Max_Bits` bits from the backward bitstream.
2. Look up the entry they index; it gives the symbol.
3. Consume only that symbol's code length.

Decode exactly the stream's literal count.

## Backward bitstreams

Huffman streams, FSE-compressed weights and the sequences bitstream are read from their last byte toward their first. The last byte is never 0. Its highest set bit is an end mark and is not data.

Number the bits so that bit `i` is bit `i % 8` of byte `i / 8`. Reading starts just below the end mark. Reading `n` bits moves the position down by `n` and returns the `n` bits above the new position as a number, with the bit at the new position as its least significant bit. Reading past the start of the stream yields zero bits.

## FSE

### Table description

This is a forward bitstream, read least significant bit first.

1. Read 4 bits; `Accuracy_Log` is that value plus 5. Let `remaining = 2^Accuracy_Log`.
2. Read probabilities for symbols 0, 1, 2, and so on, until `remaining` reaches 0:
   * Let `bits` be the bit length of `remaining + 1`, `lower = 2^(bits-1) - 1` and `threshold = 2^bits - 1 - (remaining + 1)`.
   * Read `bits` bits as `val`. If `val & lower < threshold`, give back the top bit (only `bits - 1` bits were used) and set `val = val & lower`. Otherwise, if `val > lower`, subtract `threshold` from `val`.
   * The probability is `val - 1`, where -1 means "less than one". Subtract its absolute value from `remaining`.
   * After a probability of 0, read 2 bits as a repeat count of further zero-probability symbols. While the count is 3, read another 2 bits and keep going.
3. The description ends at the next byte boundary.

### Decoding table

The table has `size = 2^Accuracy_Log` cells.

1. Symbols with probability -1 take the cells at the top of the table, from `size - 1` downward, one each.
2. Spread the other symbols, in symbol order. Each symbol of probability `p` is placed `p` times. Start at `pos = 0`, and after each placement step `pos = (pos + (size >> 1) + (size >> 3) + 3) & (size - 1)`, skipping cells taken in step 1.
3. Go through the cells in order and keep a counter per symbol. The counter starts at `p`, or at 1 for probability -1, and goes up by one each time the symbol's cell is processed. For a cell whose counter value is `d` before the increment, set `nbBits = Accuracy_Log - floor(log2 d)` and `baseline = (d << nbBits) - size`.

A state is a cell index. To decode, take the cell's symbol; the next state is `baseline + read(nbBits)`.

### Huffman weights from FSE

1. Read state 1, then state 2, each `Accuracy_Log` bits.
2. Repeat: emit the symbol of state 1 and update state 1. If that read went past the start of the stream, emit the symbol of state 2 and stop. Otherwise emit the symbol of state 2 and update state 2. If that read went past the start, emit the symbol of state 1 and stop.

## Sequences section

`Number_of_Sequences`: byte `b0`.

* 0: there are no sequences, and the block's output is just its literals.
* Less than 128: `b0`.
* Less than 255: `((b0 - 128) << 8) + b1`.
* 255: `b1 + (b2 << 8) + 0x7F00`.

Then comes the symbol compression modes byte: bits 7-6 for literal lengths (LL), 5-4 for offsets (OF), 3-2 for match lengths (ML). Each mode is one of:

* 0, predefined: use the default distribution below.
* 1, RLE: one byte follows, the only symbol, and the table has accuracy log 0.
* 2, compressed: an FSE table description follows.
* 3, repeat: reuse the previous table of that kind.

The tables follow in the order LL, OF, ML. Maximum accuracy logs are LL 9, OF 8, ML 9.

Default distributions:

* LL, accuracy log 6: `4 3 2 2 2 2 2 2 2 2 2 2 2 1 1 1 2 2 2 2 2 2 2 2 2 3 2 1 1 1 1 1 -1 -1 -1 -1`
* ML, accuracy log 6: `1 4 3 2 2 2 2 2 2 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 -1 -1 -1 -1 -1 -1 -1`
* OF, accuracy log 5: `1 1 1 1 1 1 2 2 2 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 -1 -1 -1 -1 -1`

The rest of the block is the sequences bitstream (backward).

1. Read the initial LL state, then OF, then ML, each `Accuracy_Log` bits.
2. For each sequence:
   1. Take the three codes from the current states.
   2. Read the extra bits in the order offset, match length, literal length:
      * offset value `= (1 << OF_code) + read(OF_code)`;
      * match length `= ML_base + read(ML_bits)`;
      * literal length `= LL_base + read(LL_bits)`.
   3. Then, except after the last sequence, update the states in the order LL, ML, OF.

Literal length codes:

| code | 0-15 | 16 | 17 | 18 | 19 | 20 | 21 | 22 | 23 | 24 | 25 | 26 | 27 | 28 | 29 | 30 | 31 | 32 | 33 | 34 | 35 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| base | code | 16 | 18 | 20 | 22 | 24 | 28 | 32 | 40 | 48 | 64 | 128 | 256 | 512 | 1024 | 2048 | 4096 | 8192 | 16384 | 32768 | 65536 |
| extra bits | 0 | 1 | 1 | 1 | 1 | 2 | 2 | 3 | 3 | 4 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 | 14 | 15 | 16 |

Match length codes:

| code | 0-31 | 32 | 33 | 34 | 35 | 36 | 37 | 38 | 39 | 40 | 41 | 42 | 43 | 44 | 45 | 46 | 47 | 48 | 49 | 50 | 51 | 52 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| base | code + 3 | 35 | 37 | 39 | 41 | 43 | 47 | 51 | 59 | 67 | 83 | 99 | 131 | 259 | 515 | 1027 | 2051 | 4099 | 8195 | 16387 | 32771 | 65539 |
| extra bits | 0 | 1 | 1 | 1 | 1 | 2 | 2 | 3 | 3 | 4 | 4 | 5 | 7 | 8 | 9 | 10 | 11 | 12 | 13 | 14 | 15 | 16 |

Offsets use repeat offsets `R1, R2, R3`.

* An offset value above 3 means the offset is `value - 3`, and the repeat offsets become `offset, R1, R2`.
* Otherwise, if the literal length is 0, add 1 to the value. Then:
  * value 1 means offset `R1`, with the repeat offsets unchanged;
  * value 2 means offset `R2`, and they become `R2, R1, R3`;
  * value 3 means offset `R3`, and they become `R3, R1, R2`;
  * value 4 means offset `R1 - 1`, and they become `R1 - 1, R1, R2`.

Executing a sequence: copy `literal length` bytes from the literals, then copy `match length` bytes one at a time from `offset` bytes back in the frame's output. A match may overlap the bytes it is producing. After the last sequence, copy the remaining literals.
