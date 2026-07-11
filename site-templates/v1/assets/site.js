(function () {
  /* ---------- hero video: wide, motion-safe hydration ----------
     Sources ship as data-src so phones and reduced-motion visitors fetch
     no hero video at all. Only wide, motion-ok viewports promote data-src
     to src and start playback. */
  const heroVideos = document.querySelectorAll(".hero-video");
  if (heroVideos.length) {
    const wideOk = window.matchMedia("(min-width: 721px)").matches;
    const motionOk = !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (wideOk && motionOk) {
      heroVideos.forEach((video) => {
        let hydrated = false;
        video.querySelectorAll("source[data-src]").forEach((source) => {
          source.setAttribute("src", source.getAttribute("data-src"));
          hydrated = true;
        });
        if (hydrated) {
          video.load();
          const played = video.play();
          if (played && typeof played.catch === "function") {
            played.catch(() => {});
          }
        }
      });
    }
  }

  /* ---------- mobile navigation ---------- */
  const navToggle = document.querySelector("[data-nav-toggle]");
  const nav = document.querySelector("[data-nav]");

  if (navToggle && nav) {
    navToggle.setAttribute("aria-expanded", "false");
    navToggle.addEventListener("click", () => {
      const isOpen = nav.classList.toggle("is-open");
      navToggle.setAttribute("aria-expanded", String(isOpen));
    });
  }

  /* ---------- readiness check (opening move) ----------
     All copy below is injected from the client profile by site-assembler.py. */
  const readiness = document.querySelector("[data-readiness]");
  if (readiness) {
    const priorities = {{&js.priorities_json}};
    const order = {{&js.order_json}};
    const answers = {};

    readiness.querySelectorAll(".readiness-q").forEach((row) => {
      const key = row.getAttribute("data-q");
      row.querySelectorAll("[data-answer]").forEach((btn) => {
        btn.addEventListener("click", () => {
          answers[key] = btn.getAttribute("data-answer");
          row.querySelectorAll("[data-answer]").forEach((b) =>
            b.setAttribute("aria-pressed", String(b === btn))
          );
        });
      });
    });

    const run = readiness.querySelector("[data-readiness-run]");
    const result = readiness.querySelector("[data-readiness-result]");
    const list = readiness.querySelector("[data-readiness-list]");
    const headline = readiness.querySelector("[data-readiness-headline]");

    if (run && result && list) {
      run.addEventListener("click", () => {
        const gaps = order.filter((key) => answers[key] !== "yes");
        list.innerHTML = "";

        if (gaps.length === 0) {
          if (headline)
            headline.textContent = {{&js.all_good_headline_json}};
          const li = document.createElement("li");
          li.style.animationDelay = "0ms";
          li.innerHTML = {{&js.all_good_item_html_json}};
          list.appendChild(li);
        } else {
          if (headline)
            headline.textContent =
              gaps.length === 1
                ? {{&js.gap_one_json}}
                : {{&js.gap_many_json}};
          gaps.forEach((key, i) => {
            const p = priorities[key];
            const li = document.createElement("li");
            li.style.animationDelay = i * 90 + "ms";
            li.innerHTML =
              "<strong>" + p.title + "</strong><span>" + p.note + "</span>";
            list.appendChild(li);
          });
        }

        result.hidden = false;
        run.textContent = {{&js.rerun_label_json}};
        result.scrollIntoView({ behavior: "smooth", block: "nearest" });
      });
    }
  }

  /* ---------- consultation form ---------- */
  const form = document.querySelector("[data-contact-form]");
  if (!form) return;

  const CONTACT_EMAIL = {{&js.contact_email_json}};
  const SUBJECT_PREFIX = {{&js.subject_prefix_json}};
  const status = form.querySelector("[data-form-status]");
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const data = new FormData(form);
    const name = String(data.get("name") || "").trim();
    const phone = String(data.get("phone") || "").trim();
    const email = String(data.get("email") || "").trim();
    const interest = String(data.get("interest") || "").trim();
    const message = String(data.get("message") || "").trim();

    if (!name || (!phone && !email) || !interest || !message) {
      if (status)
        status.textContent = {{&js.status_missing_json}};
      return;
    }

    const subject = encodeURIComponent(`${SUBJECT_PREFIX} ${name}`);
    const body = encodeURIComponent(
      [
        `Name: ${name}`,
        `Phone: ${phone || "Not provided"}`,
        `Email: ${email || "Not provided"}`,
        `Topic: ${interest}`,
        "",
        message,
      ].join("\n")
    );

    if (status) status.textContent = {{&js.status_opening_json}};
    window.location.href = `mailto:${CONTACT_EMAIL}?subject=${subject}&body=${body}`;
  });
})();
