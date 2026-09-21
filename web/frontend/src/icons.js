import { h } from "vue";

/* Single-stroke icon set, 24px grid. The only glyphs in the product. */
const P = {
  logo: [
    "M5 5h14v14H5z",
    "M12 12m-2.4 0a2.4 2.4 0 1 0 4.8 0a2.4 2.4 0 1 0-4.8 0",
  ],
  chat: ["M4 6h16v10H9l-5 4z"],
  gauge: ["M4 13a8 8 0 0 1 16 0", "M12 13l3.5-3.5", "M4 17h16"],
  send: ["M12 19V6", "M6 11l6-6 6 6"],
  stop: ["M8 8h8v8H8z"],
  clip: ["M8 12l6.5-6.5a3 3 0 0 1 4.2 4.2L10 18.5a5 5 0 0 1-7-7L10.5 4"],
  image: ["M4 5h16v14H4z", "M4 15l5-4 4 3 3-2 4 3", "M9.5 9.5m-1.4 0a1.4 1.4 0 1 0 2.8 0a1.4 1.4 0 1 0-2.8 0"],
  film: ["M4 5h16v14H4z", "M8 5v14", "M16 5v14", "M4 9h4", "M4 14h4", "M16 9h4", "M16 14h4"],
  wave: ["M4 12v0", "M7 9v6", "M10 6v12", "M13 9v6", "M16 4v16", "M19 9v6"],
  file: ["M6 3h8l4 4v14H6z", "M14 3v4h4", "M9 12h6", "M9 16h6"],
  x: ["M6 6l12 12", "M18 6L6 18"],
  check: ["M5 12.5l4.5 4.5L19 7.5"],
  refresh: ["M4 12a8 8 0 0 1 14-5l2 2", "M20 5v4h-4", "M20 12a8 8 0 0 1-14 5l-2-2", "M4 19v-4h4"],
  shield: ["M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z"],
  plug: ["M9 3v6", "M15 3v6", "M6 9h12v3a6 6 0 0 1-12 0z", "M12 18v3"],
  cpu: ["M7 7h10v10H7z", "M10.5 10.5h3v3h-3z", "M12 3v4", "M12 17v4", "M3 12h4", "M17 12h4"],
  user: ["M12 8m-3.4 0a3.4 3.4 0 1 0 6.8 0a3.4 3.4 0 1 0-6.8 0", "M5 20a7 7 0 0 1 14 0"],
  trash: ["M5 7h14", "M9 7V4h6v3", "M7 7l1 13h8l1-13", "M10 11v6", "M14 11v6"],
  plus: ["M12 5v14", "M5 12h14"],
  copy: ["M8 8h12v12H8z", "M16 8V4H4v12h4"],
  term: ["M4 5h16v14H4z", "M8 10l3 2.5L8 15", "M13 15h4"],
  logout: ["M14 4h6v16h-6", "M10 8l-4 4 4 4", "M6 12h9"],
  doc: ["M6 3h8l4 4v14H6z", "M14 3v4h4"],
  bolt: ["M13 3L5 13h5l-1 8 8-10h-5z"],
  eye: ["M2.5 12s3.5-6 9.5-6 9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6z", "M12 12m-2.5 0a2.5 2.5 0 1 0 5 0a2.5 2.5 0 1 0-5 0"],
  chevron: ["M8 10l4 4 4-4"],
};

export const Icon = {
  name: "Icon",
  props: { name: { type: String, required: true }, size: { type: Number, default: 17 } },
  render() {
    const paths = (P[this.name] || []).map((d) => h("path", { d }));
    return h(
      "svg",
      {
        class: "icon",
        width: this.size,
        height: this.size,
        viewBox: "0 0 24 24",
        "aria-hidden": "true",
      },
      paths,
    );
  },
};
