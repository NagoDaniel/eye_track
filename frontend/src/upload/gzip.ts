/** gzip a string with the browser's CompressionStream (PLAN §5.9). */
export async function gzip(text: string): Promise<Uint8Array> {
  const source = new Response(text).body;
  if (source === null) throw new Error("Response has no body stream");
  const compressed = source.pipeThrough(new CompressionStream("gzip"));
  return new Uint8Array(await new Response(compressed).arrayBuffer());
}
