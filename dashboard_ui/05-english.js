(function enforceEnglishInterface() {
  'use strict';

  document.documentElement.lang = 'en';
  document.title = 'WHOOP · Physiological State';

  const tablist = document.getElementById('tabs');
  if (tablist) tablist.setAttribute('aria-label', 'WHOOP Dashboard sections');

  // UI copy is authored as complete English strings in its owning component.
  // This observer is a development guard only: it reports untranslated UI
  // without mutating individual words and producing mixed-language phrases.
  const CYRILLIC = /[А-Яа-яЁё]/u;
  const PRESERVED = 'script, style, [data-preserve-language]';

  function inspectNode(root) {
    if (!root || root.nodeType !== Node.ELEMENT_NODE) return;
    if (root.matches(PRESERVED) || root.closest(PRESERVED)) return;
    const values = [];
    ['aria-label', 'title', 'placeholder'].forEach(attribute => {
      const value = root.getAttribute(attribute);
      if (value) values.push(value);
    });
    root.childNodes.forEach(node => {
      if (node.nodeType === Node.TEXT_NODE && node.nodeValue?.trim()) values.push(node.nodeValue);
    });
    if (values.some(value => CYRILLIC.test(value))) {
      root.dataset.localizationStatus = 'untranslated';
    } else {
      delete root.dataset.localizationStatus;
    }
  }

  document.querySelectorAll('main *, [role="dialog"] *').forEach(inspectNode);
  new MutationObserver(mutations => mutations.forEach(mutation => {
    if (mutation.type === 'attributes') inspectNode(mutation.target);
    mutation.addedNodes.forEach(node => {
      if (node.nodeType !== Node.ELEMENT_NODE) return;
      inspectNode(node);
      node.querySelectorAll?.('*').forEach(inspectNode);
    });
  })).observe(document.body, {
    childList: true,
    subtree: true,
    attributes: true,
    attributeFilter: ['aria-label', 'title', 'placeholder']
  });
})();
