const navbarToggle = document.getElementById("navbarToggle");
const navbarMenu = document.getElementById("navbarMenu");

if (navbarToggle && navbarMenu) {
  navbarToggle.addEventListener("click", () => {
    navbarToggle.classList.toggle("is-open");
    navbarMenu.classList.toggle("is-open");
  });
}


 (function () {
        function closeAppMessageModal() {
          const modal = document.getElementById("app-message-modal");

          if (!modal) return;

          modal.classList.add("is-closing");

          setTimeout(function () {
            modal.remove();
            document.body.classList.remove("message-modal-open");
          }, 220);
        }

        document.addEventListener("DOMContentLoaded", function () {
          const modal = document.getElementById("app-message-modal");

          if (!modal) return;

          document.body.classList.add("message-modal-open");

          document.addEventListener("click", function (event) {
            const closeTarget = event.target.closest(
              "[data-message-modal-close]",
            );

            if (closeTarget) {
              event.preventDefault();
              closeAppMessageModal();
            }
          });

          document.addEventListener("keydown", function (event) {
            if (event.key === "Escape") {
              closeAppMessageModal();
            }
          });
        });
      })();