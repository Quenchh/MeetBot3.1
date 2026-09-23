// ──────────────────────────────────────────────────────────────
//  tailwind.config.js — MeetBot web arayüzü (Tailwind CSS v3)
//
//  Derleme:  npm run build:css   (çıktı: static/tailwind.css, repoda tutulur)
//  İzleme:   npm run watch:css
//
//  Vaporwave renkleri static/src/input.css içindeki CSS değişkenlerine
//  bağlıdır; "rgb(var(--x-rgb) / <alpha-value>)" sayesinde bg-teal/10 gibi
//  opaklık kısaltmaları da çalışır.
// ──────────────────────────────────────────────────────────────

/** @type {import('tailwindcss').Config} */
module.exports = {
    content: ["./static/index.html", "./static/app.js"],
    theme: {
        extend: {
            colors: {
                teal: "rgb(var(--teal-rgb) / <alpha-value>)",
                fuchsia: "rgb(var(--fuchsia-rgb) / <alpha-value>)",
                sunset: "rgb(var(--sunset-rgb) / <alpha-value>)",
                purple: "rgb(var(--purple-rgb) / <alpha-value>)",
            },
            fontFamily: {
                display: ['"Saira Condensed"', '"Arial Narrow"', "sans-serif"],
                vt: ["VT323", "ui-monospace", "monospace"],
            },
        },
    },
    plugins: [],
};
