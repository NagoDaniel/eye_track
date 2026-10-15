/** Ask the browser to warn before the page closes while uploads are pending (PLAN §5.2 step 11). Returns a remover. */
export function installUnloadGuard(isPending: () => boolean, target: Window = window): () => void {
  const handler = (event: BeforeUnloadEvent): void => {
    if (!isPending()) return;
    event.preventDefault();
    event.returnValue = ""; // legacy browsers need this to show the dialog
  };
  target.addEventListener("beforeunload", handler);
  return () => target.removeEventListener("beforeunload", handler);
}
