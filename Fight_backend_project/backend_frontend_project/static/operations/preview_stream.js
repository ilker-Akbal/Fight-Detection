/* Framed JPEG stream: ASCII byte-length + camera ID + newline, then JPEG.
   Bounded framing, independent of fetch/TCP chunk boundaries. */
class CameraFrameParser {
  constructor(deliver) {this.deliver = deliver; this.buffer = new Uint8Array(0); this.pending = null;}
  feed(chunk) {
    if (this.buffer.length + chunk.length > 8 * 1024 * 1024 + 65536) throw new Error('Oversized preview');
    const combined = new Uint8Array(this.buffer.length + chunk.length);
    combined.set(this.buffer); combined.set(chunk, this.buffer.length); this.buffer = combined;
    while (this.buffer.length) {
      if (!this.pending) {
        const end = this.buffer.indexOf(10);
        if (end < 0) {if (this.buffer.length > 128) throw new Error('Invalid preview header'); return;}
        if (end > 128) throw new Error('Invalid preview header');
        const match = /^(\d+) ([A-Za-z0-9_.-]{1,100})$/.exec(new TextDecoder().decode(this.buffer.subarray(0, end)));
        if (!match || Number(match[1]) < 1 || Number(match[1]) > 8 * 1024 * 1024) throw new Error('Invalid preview size');
        this.pending = {size: Number(match[1]), camera: match[2]};
        this.buffer = this.buffer.subarray(end + 1);
      }
      if (this.buffer.length < this.pending.size) return;
      this.deliver(this.pending.camera, this.buffer.slice(0, this.pending.size));
      this.buffer = this.buffer.subarray(this.pending.size); this.pending = null;
    }
  }
}
if (typeof module !== 'undefined') module.exports = CameraFrameParser;
