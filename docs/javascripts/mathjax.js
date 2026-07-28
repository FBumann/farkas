// pymdownx.arithmatex in `generic: true` mode rewrites `$...$` / `$$...$$` in
// the source into `\(...\)` / `\[...\]` inside a `.arithmatex` element, so
// those are the delimiters MathJax is told to look for — and it is told to
// look nowhere else, which keeps a `$` in a shell block from becoming math.
window.MathJax = {
  tex: {
    inlineMath: [["\\(", "\\)"]],
    displayMath: [["\\[", "\\]"]],
    processEscapes: true,
    processEnvironments: true,
  },
  options: {
    ignoreHtmlClass: ".*|",
    processHtmlClass: "arithmatex",
  },
};
