// Small DOM helpers. Text is always set through textContent, never innerHTML.

export interface ElOptions {
  class?: string;
  text?: string;
  attrs?: Record<string, string>;
}

export function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  options: ElOptions = {},
  ...children: (Node | string)[]
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (options.class !== undefined) node.className = options.class;
  if (options.text !== undefined) node.textContent = options.text;
  for (const [name, value] of Object.entries(options.attrs ?? {})) node.setAttribute(name, value);
  node.append(...children);
  return node;
}

/** Replace everything in `root` with `nodes`. */
export function render(root: HTMLElement, ...nodes: Node[]): void {
  root.replaceChildren(...nodes);
}

export function button(label: string, onClick: () => void): HTMLButtonElement {
  const node = el("button", { text: label, attrs: { type: "button" } });
  node.addEventListener("click", onClick);
  return node;
}

/** A card with a heading, for the ordinary text screens. */
export function card(title: string, ...children: (Node | string)[]): HTMLElement {
  return el("section", { class: "card" }, el("h1", { text: title }), ...children);
}

/** A paragraph. */
export function p(text: string, className?: string): HTMLParagraphElement {
  return el("p", className === undefined ? { text } : { text, class: className });
}

/** A screen the participant cannot leave (unsupported browser, failed integrity check, ...). */
export function blockForever(): Promise<never> {
  return new Promise<never>(() => undefined);
}
