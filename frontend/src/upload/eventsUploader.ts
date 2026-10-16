// The events stream uploader (PLAN §5.9): JSON lines `{t, type, data}`, see JsonlUploader.

import type { EventRecord } from "../eventLog";
import { JsonlUploader } from "./jsonlUploader";
import type { UploadQueue } from "./uploadQueue";

export class EventsUploader extends JsonlUploader<EventRecord> {
  constructor(
    queue: Pick<UploadQueue, "enqueue">,
    chunkMs: number,
    onError: (error: Error) => void,
  ) {
    super(queue, "events", chunkMs, onError);
  }
}
