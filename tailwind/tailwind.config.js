/** Panelin stil yapılandırması (eski CDN ayarlarının aynısı). */
module.exports = {
  // Sınıf adları şablonlarda, JS'te ve Python'da (ör. app.py STATUS_META) geçer.
  content: ["../templates/**/*.html", "../static/**/*.js", "../*.py"],
  theme: {
    extend: {
      colors: {
        brand: {
          50: "#ecfdf5", 100: "#d1fae5", 200: "#a7f3d0", 300: "#6ee7b7",
          400: "#34d399", 500: "#10b981", 600: "#059669", 700: "#047857",
          800: "#065f46", 900: "#064e3b",
        },
      },
      boxShadow: {
        soft: "0 10px 30px -12px rgba(15, 23, 42, 0.18)",
      },
    },
  },
  plugins: [],
};
