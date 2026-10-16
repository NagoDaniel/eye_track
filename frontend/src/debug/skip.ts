// Debug mode `S` (PLAN §12.1): ask the screen that is running (calibration, validation or video)
// to end right away. Screens subscribe while they run; only debug mode ever fires it.

export interface SkipSignal {
  /** Call `listener` when a skip is requested; returns the function that unsubscribes. */
  onSkip(listener: () => void): () => void;
}

export class SkipBus implements SkipSignal {
  private readonly listeners = new Set<() => void>();

  onSkip(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  /** Tell every running screen to end. Returns how many were listening. */
  fire(): number {
    const current = [...this.listeners];
    for (const listener of current) listener();
    return current.length;
  }
}
