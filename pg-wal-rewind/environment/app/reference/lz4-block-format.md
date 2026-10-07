# LZ4 block format

PostgreSQL stores an `lz4`-compressed datum as one raw LZ4 block (no frame header, no checksum). A block is a series of sequences. Each sequence is:

1. A token byte. Its high 4 bits are the literal length, its low 4 bits the match length minus 4.
2. If the literal length is 15, more length bytes follow: each is added to it, and the run stops after the first byte that is not 255.
3. The literal bytes, copied to the output as they are.
4. A 2-byte little-endian offset (1 to 65535): how far back in the output the match starts.
5. If the match length field is 15, more length bytes follow, read the same way as for literals. The match length is the field total plus 4.
6. The match: that many bytes copied one at a time from `offset` bytes back in the output, so a match may overlap the bytes it is producing.

The last sequence of a block has only a token and literals; the block ends after its literals.
