/* Esnaf Randevu Asistanı — küçük arayüz davranışları (bağımlılıksız). */
(function () {
  "use strict";

  // --- Bildirim kutularını otomatik kapat ---------------------------------
  function bindToast(toast) {
    var closeBtn = toast.querySelector(".toast-close");
    if (closeBtn) {
      closeBtn.addEventListener("click", function () {
        toast.remove();
      });
    }
    window.setTimeout(function () {
      toast.style.transition = "opacity .35s ease, transform .35s ease";
      toast.style.opacity = "0";
      toast.style.transform = "translateY(-6px)";
      window.setTimeout(function () {
        toast.remove();
      }, 400);
    }, 6500);
  }
  document.querySelectorAll("#toasts .toast").forEach(bindToast);

  // --- Silme onayı --------------------------------------------------------
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      var message = form.getAttribute("data-confirm") || "Emin misiniz?";
      if (!window.confirm(message)) {
        event.preventDefault();
      }
    });
  });

  // --- Açılır menüleri dışarı tıklayınca kapat ----------------------------
  document.querySelectorAll("details[data-menu]").forEach(function (details) {
    document.addEventListener("click", function (event) {
      if (details.open && !details.contains(event.target)) {
        details.open = false;
      }
    });
    details.addEventListener("toggle", function () {
      if (!details.open) return;
      document.querySelectorAll("details[data-menu][open]").forEach(function (other) {
        if (other !== details) other.open = false;
      });
    });
  });

  // --- Ayarlar: HTTP alanlarını yalnızca HTTP sağlayıcısında göster -------
  (function () {
    var provider = document.getElementById("whatsapp_provider");
    var httpFields = document.getElementById("http-fields");
    if (!provider || !httpFields) return;
    function sync() {
      // Alanları gizle ama devre dışı bırakma: başka sağlayıcıya geçince
      // girilmiş adres/token kaybolmasın.
      httpFields.classList.toggle("hidden", provider.value !== "http");
    }
    provider.addEventListener("change", sync);
    sync();
  })();

  // --- Tarih kutusu: geçmiş zaman seçilemesin -----------------------------
  document.querySelectorAll('input[type="datetime-local"][data-min]').forEach(function (input) {
    var min = input.getAttribute("data-min");
    if (min) input.min = min;
  });
})();