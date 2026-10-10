// test-helpers.js — Shared DOM stub for renderer and editor tests.
// Imported by both test-render.js and test-editor.js.
//
// The stub mimics the real browser DOM API so that tests catch mismatches
// between what editor.js reads and what a real HTMLElement exposes:
//   - Elements: nodeType 1, tagName UPPERCASE, no "tag" property.
//   - .children: HTMLCollection-like — length, item(n), indexed access,
//     iterable via for-of, but NO .find, .filter, or .map.
//   - .childNodes: NodeList-like — all child nodes (elements + text),
//     length, item(n), indexed access, iterable; no array methods.
//   - .textContent: recursive text-content getter, same semantics as the
//     real DOM. Not settable (editor.js never sets it).
//   - .attributes: NamedNodeMap-like — iterable {name, value} pairs, in the
//     order attributes were set.
//   - appendChild/insertBefore: move a node already attached elsewhere,
//     removing it from its old parent first, matching real DOM semantics.
//   - Text nodes: nodeType 3, .data property (NOT .text).
//   - innerHTML: throws on get and set.
//   - getSelection()/createRange(): only the boundary-point properties
//     editor.js's formatting commands read (startContainer, startOffset,
//     endContainer, endOffset, collapsed) — real, readonly properties on a
//     browser Range, so editor.js never calls a Range or Selection *method*
//     the stub would need to reimplement (surroundContents, extractContents).

export function makeDOMStub() {
  let currentRange = null;
  function makeHTMLCollection(elems) {
    const view = {
      length: elems.length,
      item(n) { return elems[n] ?? null; },
      [Symbol.iterator]() { return elems[Symbol.iterator](); },
    };
    for (let i = 0; i < elems.length; i++) view[i] = elems[i];
    return view;
  }

  function makeNodeList(nodes) {
    const view = {
      length: nodes.length,
      item(n) { return nodes[n] ?? null; },
      [Symbol.iterator]() { return nodes[Symbol.iterator](); },
    };
    for (let i = 0; i < nodes.length; i++) view[i] = nodes[i];
    return view;
  }

  function createElement(tagName) {
    const _childNodes = [];

    const elem = {
      nodeType: 1,
      tagName: tagName.toUpperCase(),
      attrs: {},
      parentElement: null,

      get childNodes() {
        return makeNodeList(_childNodes.slice());
      },
      get children() {
        return makeHTMLCollection(_childNodes.filter(n => n.nodeType === 1));
      },
      get textContent() {
        return _childNodes.map(n =>
          n.nodeType === 3 ? n.data : (n.textContent ?? '')
        ).join('');
      },
      get attributes() {
        return Object.keys(this.attrs).map(name => ({ name, value: this.attrs[name] }));
      },

      setAttribute(k, v) { this.attrs[k] = String(v); },
      getAttribute(k) {
        return Object.prototype.hasOwnProperty.call(this.attrs, k)
          ? this.attrs[k]
          : null;
      },
      removeAttribute(k) { delete this.attrs[k]; },
      appendChild(child) {
        if (child && typeof child === 'object' && child.parentElement
          && child.parentElement !== this) {
          child.parentElement.removeChild(child);
        }
        _childNodes.push(child);
        if (child && typeof child === 'object') child.parentElement = this;
        return child;
      },
      removeChild(child) {
        const idx = _childNodes.indexOf(child);
        if (idx !== -1) {
          _childNodes.splice(idx, 1);
          if (child && typeof child === 'object') child.parentElement = null;
        }
        return child;
      },
      insertBefore(newNode, refNode) {
        if (newNode && typeof newNode === 'object' && newNode.parentElement) {
          newNode.parentElement.removeChild(newNode);
        }
        if (refNode == null) {
          _childNodes.push(newNode);
        } else {
          const idx = _childNodes.indexOf(refNode);
          if (idx === -1) _childNodes.push(newNode);
          else _childNodes.splice(idx, 0, newNode);
        }
        if (newNode && typeof newNode === 'object') newNode.parentElement = this;
        return newNode;
      },
      replaceChild(newChild, oldChild) {
        const idx = _childNodes.indexOf(oldChild);
        if (idx === -1) return oldChild;
        if (newChild && typeof newChild === 'object' && newChild.parentElement
          && newChild.parentElement !== this) {
          newChild.parentElement.removeChild(newChild);
        }
        _childNodes[idx] = newChild;
        if (newChild && typeof newChild === 'object') newChild.parentElement = this;
        if (oldChild && typeof oldChild === 'object') oldChild.parentElement = null;
        return oldChild;
      },
      _listeners: null,
      addEventListener(type, handler) {
        if (!this._listeners) this._listeners = {};
        if (!this._listeners[type]) this._listeners[type] = [];
        this._listeners[type].push(handler);
      },
      dispatchEvent(event) {
        const handlers = (this._listeners && this._listeners[event.type]) || [];
        for (const h of handlers) h(event);
      },
    };

    Object.defineProperty(elem, 'innerHTML', {
      get() { throw new Error('innerHTML must not be used — use createTextNode'); },
      set() { throw new Error('innerHTML must not be used — use createTextNode'); },
    });

    return elem;
  }

  function createTextNode(data) {
    return { nodeType: 3, data };
  }

  // A plain object exposing the same boundary-point properties as a real
  // Range: startContainer/startOffset/endContainer/endOffset, and a computed
  // .collapsed. setStart/setEnd are real Range methods too, kept here only as
  // a construction convenience — editor.js never calls them.
  function createRange() {
    const range = {
      startContainer: null,
      startOffset: 0,
      endContainer: null,
      endOffset: 0,
      get collapsed() {
        return range.startContainer === range.endContainer
          && range.startOffset === range.endOffset;
      },
      setStart(node, offset) { range.startContainer = node; range.startOffset = offset; },
      setEnd(node, offset) { range.endContainer = node; range.endOffset = offset; },
    };
    return range;
  }

  // document.getSelection(): only rangeCount/getRangeAt/addRange/removeAllRanges,
  // which is all editor.js reads or calls.
  function getSelection() {
    return {
      get rangeCount() { return currentRange ? 1 : 0; },
      get isCollapsed() { return !currentRange || currentRange.collapsed; },
      getRangeAt(i) {
        if (i !== 0 || !currentRange) throw new Error('IndexSizeError: no such range');
        return currentRange;
      },
      addRange(r) { currentRange = r; },
      removeAllRanges() { currentRange = null; },
    };
  }

  return { createElement, createTextNode, createRange, getSelection };
}

// Select from startOffset in startNode to endOffset in endNode (the same node
// for a same-text-node selection) — a test-only convenience over
// createRange()/getSelection(), not a DOM API of its own.
export function selectRange(stub, startNode, startOffset, endNode, endOffset) {
  const range = stub.createRange();
  range.setStart(startNode, startOffset);
  range.setEnd(endNode, endOffset);
  stub.getSelection().addRange(range);
  return range;
}
