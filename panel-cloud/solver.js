// Speexx AutoCycle - optimized.js
//
// Patches CourseExercisesControlsView to register itself on
// window.__speexxAutoCycleControls, and installs a global controller that
// drives solve -> correct -> next across exercises, skips no-Next exercises
// (speaking tests), and advances across packet boundaries.
//
// The view patch + controller + resume shim all live inside ONE named
// function so Function.prototype.toString() captures the whole thing. The
// controller stores its own source in localStorage; after a full page reload
// (packet-boundary Continue) the resume shim re-evaluates the ENTIRE script -
// view patch included - so the cycle keeps running on a patched view.

function __speexxAutoCycleMain() {
  var SRC_KEY = "__speexxAutoCycleSrc";
  var RESUME_KEY = "__speexxAutoCycleResume";

  try {
    localStorage.setItem(SRC_KEY, "(" + __speexxAutoCycleMain.toString() + ")();");
  } catch (e) {
    console.log("[AutoCycle] could not store source: " + e);
  }

  // eval stored source, self-healing older "function(){}();" copies that are
  // SyntaxErrors by re-wrapping as an IIFE "(function(){...})();".
  function tryEval(src) {
    try {
      (0, eval)(src);
      return true;
    } catch (e) {
      if (!(e instanceof SyntaxError)) {
        console.log("[AutoCycle] resume eval error: " + e);
        return false;
      }
      try {
        var fixed = "(" + src.replace(/\(\)\s*;\s*$/, "") + ")();";
        (0, eval)(fixed);
        try { localStorage.setItem(SRC_KEY, fixed); } catch (healErr) {}
        return true;
      } catch (e2) {
        console.log("[AutoCycle] resume eval error (healed): " + e2);
        return false;
      }
    }
  }

  // Don't start a second controller if one is already alive on this page.
  if (window.__speexxAutoCycleController && window.__speexxAutoCycleController._alive) {
    return;
  }

  // Re-arm on every page load. The listener dies with the page, but the stored
  // source re-registers it on the next page via the resume path below.
  function onLoadResume() {
    if (localStorage.getItem(RESUME_KEY) !== "1") return;
    if (window.__speexxAutoCycleController && window.__speexxAutoCycleController._alive) return;
    var src = localStorage.getItem(SRC_KEY);
    if (src) tryEval(src);
  }
  if (window.addEventListener) {
    window.addEventListener("load", onLoadResume);
    window.addEventListener("pageshow", onLoadResume);
    window.addEventListener("beforeunload", function () {
      if (window.__speexxAutoCycleController && window.__speexxAutoCycleController._alive) {
        localStorage.setItem(RESUME_KEY, "1");
      }
    });
  }

  // ── View class patch (re-applied on every run, including resume) ──────────
  // Replaces the app's view so initialize() registers the live view on
  // window.__speexxAutoCycleControls for the controller to drive. Inside this
  // function so the stored source re-applies it after a reload.
  if (
    typeof CourseWare !== "undefined" &&
    CourseWare.CourseExercises &&
    typeof Backbone !== "undefined" &&
    Backbone.Speexx &&
    Backbone.Speexx.HandlebarsView
  ) {
    CourseWare.CourseExercises.CourseExercisesControlsView =
      Backbone.Speexx.HandlebarsView.extend({
        templateName: "cw-language-course-controls",
        className: "exercise-controls",

        initialize: function (options) {
          var exerciseView = (this.exerciseView = options.exerciseView),
            model = exerciseView.model;

          this.firstTime = true;
          this.solutionShown = false;
          this.modified = false;
          this.dialogEnded = false;

          this.listenTo(exerciseView, "modified", function () {
            if (!this.modified) {
              this.modified = true;
              this.render();
            }
          });

          this.listenToOnce(exerciseView, "dialog:ended", function () {
            this.dialogEnded = true;
            this.render();
          });

          this.listenTo(exerciseView, "static:added static:removed", this.render);

          this.listenTo(model, "sync", function (m, resp, opts) {
            var wasSilent = !!this.silent;
            this.silent = !!opts.silent;
            if (!this.silent || !wasSilent) this.render();
          });

          this.listenTo(CourseWare.Language, "change:textpool", this.render);

          this.on("render:after", function () {
            this.$(".btn.btn-link[title]").tooltip();
          });

          // Passive registration - the global controller owns the cycle.
          window.__speexxAutoCycleControls = this;
        },

        onDestroy: function () {
          if (window.__speexxAutoCycleControls === this) {
            window.__speexxAutoCycleControls = null;
          }
        },

        templateModel: function () {
          var exerciseView = this.exerciseView,
            model = exerciseView.model,
            type = model.get("type"),
            result = this.silent ? null : model.get("result"),
            hasResult =
              !exerciseView.static && type.hasResult && !type.pronunciation,
            hasSolution =
              !this.solutionShown &&
              hasResult &&
              !this.firstTime &&
              result !== 100;

          return {
            firstTime: this.firstTime,
            hasCorrect: hasResult,
            hasSolution: hasSolution,
            hideResult: exerciseView.static || !type.hasResult,
            dialogEnded: this.dialogEnded,
            modified: this.modified,
            solutionShown: this.solutionShown,
            result: result,
          };
        },

        events: {
          "click .btn.correct": function () {
            CourseWare.Audio.stop();
            this.trigger("correct");
          },
          "click .btn.repeat": function () {
            this.exerciseView.model.get("staticBefore")
              ? this.exerciseView.setStatic()
              : this.exerciseView.render();
          },
          "click .btn.next": function () {
            this.trigger("next");
          },
          "keypress .btn.next": function (e) {
            e.preventDefault();
            var code = e.originalEvent.charCode;
            if (code === 13 || code === 32) this.trigger("nextKeyboard");
          },
          "click .btn.solution": function () {
            this.render();
            this.trigger("solve");
          },
        },
      });
    console.log("[AutoCycle] view class patched");
  } else {
    console.log("[AutoCycle] WARN: CourseWare/Backbone not ready - using direct-click fallback");
  }

  // ── Controller (view-lifecycle independent) ──────────────────────────────
  // Self-contained: defines its own delays so it works even if the view patch
  // didn't run (Backbone not ready).
  var delays = {
    init: 500,
    warmup: 700,
    correct: 5000,
    next: 10000,
    reset: 3000,
    skipPoll: 600,
    skipMaxAttempts: 8,
  };

  var timers = [],
    stopped = false,
    cycleStarted = false;

  window.__speexxAutoCycleController = {
    _alive: true,
    stop: function () {
      stopped = true;
      this._alive = false;
      timers.forEach(clearTimeout);
      timers = [];
      try { localStorage.removeItem(RESUME_KEY); } catch (e) {}
      console.log("[AutoCycle] stopped (resume flag cleared)");
    },
  };

  function addTimer(fn, delay) {
    var id = setTimeout(function () {
      var idx = timers.indexOf(id);
      if (idx !== -1) timers.splice(idx, 1);
      if (stopped) return;
      try {
        fn();
      } catch (timerErr) {
        // A throw in a timer callback used to silently kill the whole chain.
        console.log("[AutoCycle] timer error: " + timerErr);
        if (timerErr && timerErr.stack) console.log(timerErr.stack);
      }
    }, delay);
    timers.push(id);
    return id;
  }

  function getExerciseOrdinal() {
    function lastExerciseNumber(str) {
      if (!str) return null;
      var matches = str.match(/\/exercise\/(\d+)/g);
      if (!matches) return null;
      var num = matches[matches.length - 1].match(/(\d+)/);
      return num ? parseInt(num[1], 10) : null;
    }
    return (
      lastExerciseNumber(window.location.pathname) ||
      lastExerciseNumber(window.location.hash) ||
      lastExerciseNumber(window.location.href) ||
      null
    );
  }

  function clickExerciseOrdinal(ordinal) {
    var selector = '.exercise-icon[data-ordinal="' + ordinal + '"]',
      $target = $(selector).filter(":visible").first();
    if (!$target.length) $target = $(selector).first();
    if ($target.length) {
      if ($target[0].click) $target[0].click();
      else $target.trigger("click");
      return true;
    }
    return false;
  }

  function clickControl(selector) {
    var $target = $(selector).filter(":visible").first();
    if (!$target.length) $target = $(selector).first();
    if (!$target.length) return false;
    if ($target[0].click) $target[0].click();
    else $target.trigger("click");
    return true;
  }

  function waitForOrdinal(ordinal, callback, attempts) {
    attempts = attempts === undefined ? 30 : attempts;
    if (getExerciseOrdinal() === ordinal || attempts <= 0) {
      callback();
      return;
    }
    addTimer(function () { waitForOrdinal(ordinal, callback, attempts - 1); }, 250);
  }

  // Wait until the ordinal changes away from oldOrdinal (null => wait for any
  // real ordinal), so the next cycle runs on the new exercise, not a destroyed view.
  function waitForNewOrdinal(oldOrdinal, callback, attempts) {
    attempts = attempts === undefined ? 40 : attempts;
    var current = getExerciseOrdinal();
    var changed = oldOrdinal === null ? current !== null : current !== oldOrdinal;
    if (changed || attempts <= 0) {
      callback();
      return;
    }
    addTimer(function () { waitForNewOrdinal(oldOrdinal, callback, attempts - 1); }, 250);
  }

  // Advance to the next exercise: click .next (view-scoped then global), poll
  // if it may still render, then skip via pager icon or page-level advance.
  // `done` is always called once; a watchdog guarantees the chain never dies.
  function advance(currentOrdinal, done) {
    var finished = false;
    function finish() {
      if (!finished) { finished = true; done(); }
    }
    addTimer(finish, delays.skipPoll * delays.skipMaxAttempts + 8000);

    function tryNext(attempt) {
      if (finished) return;
      var view = window.__speexxAutoCycleControls,
        clicked = false;

      if (view && !view._isDestroyed && view.$) {
        var $vNext = view.$(".next");
        if ($vNext.length) {
          if ($vNext[0].click) $vNext[0].click();
          else $vNext.click();
          clicked = true;
        }
      }
      if (!clicked) {
        clicked = clickControl(".exercise-controls .next, .btn.next, .next");
      }

      if (clicked) {
        console.log("[AutoCycle] #" + currentOrdinal + " -> Next");
        waitForNewOrdinal(currentOrdinal, finish);
        return;
      }

      if (attempt < delays.skipMaxAttempts) {
        addTimer(function () { tryNext(attempt + 1); }, delays.skipPoll);
        return;
      }

      // No in-exercise .next after polling: skip via pager icon, else advance
      // the packet via a page-level Next/Continue.
      var nextOrdinal = (currentOrdinal === null ? 0 : currentOrdinal) + 1;
      console.log("[AutoCycle] #" + currentOrdinal + " no Next -> looking for advance");

      if (clickExerciseOrdinal(nextOrdinal)) {
        console.log("[AutoCycle] -> next exercise #" + nextOrdinal + " (pager)");
        waitForOrdinal(nextOrdinal, finish);
        return;
      }

      var pageNextSelectors = [
        ".btn-next", ".btn.next", "button.next", "a.next",
        ".continue", ".btn-continue", ".pager .next",
        ".next:not(.exercise-controls .next)",
        '[data-action="next"]', '[data-action="continue"]',
      ];
      for (var i = 0; i < pageNextSelectors.length; i++) {
        if (clickControl(pageNextSelectors[i])) {
          console.log("[AutoCycle] -> packet advance via " + pageNextSelectors[i]);
          waitForNewOrdinal(currentOrdinal, finish);
          return;
        }
      }

      // Last resort: bump standard-packet/<n> in the URL and navigate.
      var m = window.location.pathname.match(/\/standard-packet\/(\d+)\/exercise\/(\d+)/);
      if (m) {
        var nextPacket = parseInt(m[1], 10) + 1;
        var nextPath = window.location.pathname.replace(
          /\/standard-packet\/\d+\/exercise\/\d+/,
          "/standard-packet/" + nextPacket + "/exercise/1",
        );
        console.log("[AutoCycle] no advance button -> URL bump -> " + nextPath);
        if (window.history && window.history.pushState) {
          window.history.pushState({}, "", nextPath);
        }
        window.location.pathname = nextPath;
        addTimer(finish, 2000);
        return;
      }

      console.log("[AutoCycle] could not advance - retrying cycle");
      finish();
    }
    tryNext(0);
  }

  function runCycle() {
    var view = window.__speexxAutoCycleControls,
      ordinal = getExerciseOrdinal();

    console.log("[AutoCycle] cycle #" + ordinal + (view && !view._isDestroyed ? "" : " (no view)"));

    // NULL ORDINAL: URL has no /exercise/<n> (mid-navigation or packet root).
    // Don't click .next blind (that caused the "#null -> Next" loop).
    if (ordinal === null) {
      var nullPath = window.location.pathname;
      var onPacketRoot =
        /\/standard-packet\/\d+(?!\/exercise\/)/.test(nullPath) &&
        !/\/exercise\//.test(nullPath);

      if (onPacketRoot) {
        // Packet summary page (e.g. "You completed unit N" + Continue). A bare
        // pushState doesn't trigger the SPA router, so click the page's own
        // Continue/Next button.
        var summarySelectors = [
          ".btn.btn-primary", "a.btn.btn-primary", "button.btn-primary",
          ".btn-continue", "a.btn-continue", ".continue", "a.continue",
          ".btn-next", "a.btn-next", ".next", "a.next", "button.next",
          '[data-action="next"]', '[data-action="continue"]',
        ];
        var summaryClicked = false;
        for (var si = 0; si < summarySelectors.length; si++) {
          if (clickControl(summarySelectors[si])) {
            console.log("[AutoCycle] -> packet summary advance via " + summarySelectors[si]);
            summaryClicked = true;
            break;
          }
        }

        if (!summaryClicked) {
          // Text fallback: a visible button whose text is Continue/Next/etc.
          var btns = document.querySelectorAll(
            'button, a.btn, .btn, a[role="button"], input[type="submit"]',
          );
          var txtRe = /^(continue|next|weiter|avanti|suivant|siguiente|volgende|fortsätt|fortsett|dalej|næste|jätka|jatk|naprijed|tovább|következő|előre|ilguri|parādit|turpināt|jagatege|nastavi|prodolži|naprej|napredu|slijedeće|前方|次へ|계속|继续|다음|下一步)\b/i;
          for (var bi = 0; bi < btns.length; bi++) {
            var el = btns[bi];
            if (el.offsetParent === null) continue;
            var txt = (el.textContent || el.value || "").trim();
            if (txt && txtRe.test(txt)) {
              if (el.click) el.click();
              else $(el).trigger("click");
              console.log('[AutoCycle] -> packet summary advance via text: "' + txt.slice(0, 30) + '"');
              summaryClicked = true;
              break;
            }
          }
        }

        // After clicking (or attempting), wait for the next packet's exercise 1.
        // If the click didn't navigate, fall back to a direct URL bump.
        addTimer(function () {
          waitForNewOrdinal(null, function () {
            var newO = getExerciseOrdinal();
            if (newO !== null) {
              console.log("[AutoCycle] packet-root -> URL changed to #" + newO);
              addTimer(runCycle, delays.reset);
              return;
            }
            var pm2 = window.location.pathname.match(/\/standard-packet\/(\d+)/);
            if (!pm2) {
              console.log("[AutoCycle] packet-root: cannot parse packet, retrying");
              addTimer(runCycle, delays.reset);
              return;
            }
            var np = parseInt(pm2[1], 10) + 1;
            var npath = window.location.pathname.replace(/\/standard-packet\/\d+/, "/standard-packet/" + np);
            if (!/\/exercise\//.test(npath)) npath = npath.replace(/\/$/, "") + "/exercise/1";
            console.log("[AutoCycle] packet-root click failed -> URL bump -> " + npath);
            if (window.history && window.history.pushState) window.history.pushState({}, "", npath);
            window.location.pathname = npath;
            addTimer(function () {
              waitForNewOrdinal(null, function () {
                var o2 = getExerciseOrdinal();
                console.log("[AutoCycle] post-bump URL -> #" + o2 + (o2 === null ? " (still null, retrying)" : ""));
                addTimer(runCycle, delays.reset);
              });
            }, 1500);
          });
        }, 1500);
        return;
      }

      // Not a packet root: poll for the URL to settle back to a real ordinal.
      var nullAttempts = 0;
      function waitForSettle() {
        var o = getExerciseOrdinal();
        if (o !== null) {
          console.log("[AutoCycle] URL settled -> #" + o);
          addTimer(runCycle, 500);
          return;
        }
        if (++nullAttempts > 20) {
          console.log("[AutoCycle] stuck at null (not packet root) -> attempting advance");
          advance(null, function () { addTimer(runCycle, delays.reset); });
          return;
        }
        addTimer(waitForSettle, 250);
      }
      addTimer(waitForSettle, 250);
      return;
    }

    // solve -> correct, but skip pronunciation/speaking types (no getResult()).
    var canSolve = true;
    try {
      if (view && !view._isDestroyed && view.exerciseView && view.exerciseView.model) {
        var type = view.exerciseView.model.get("type");
        if (type && type.pronunciation) {
          canSolve = false;
          console.log("[AutoCycle] #" + ordinal + " pronunciation -> skip solve/correct");
        }
      }
    } catch (e) {}

    if (canSolve) {
      try {
        if (view && !view._isDestroyed && view.trigger) {
          view.trigger("solve");
        } else {
          clickControl(".exercise-controls .solution, .btn.solution, .solution");
        }
      } catch (err) {
        console.log("[AutoCycle] solve error: " + err);
      }
      addTimer(function () {
        try {
          if (view && !view._isDestroyed && view.trigger) {
            view.trigger("correct");
          } else {
            clickControl(".exercise-controls .correct, .btn.correct, .correct");
          }
        } catch (err) {
          console.log("[AutoCycle] correct error: " + err);
        }
      }, delays.correct);
    }

    addTimer(function () {
      advance(ordinal, function () { addTimer(runCycle, delays.reset); });
    }, delays.next);
  }

  function startCycle() {
    if (cycleStarted || stopped) return;
    cycleStarted = true;
    addTimer(runCycle, delays.init);
  }

  // Boot: on exercise 1 the first view doesn't fully wire up, so prime it by
  // bouncing 1 -> 2 -> 1, then start. Any other ordinal starts directly.
  function boot() {
    var ordinal = getExerciseOrdinal();
    if (ordinal !== 1) {
      startCycle();
      return;
    }
    addTimer(function () {
      if (!clickExerciseOrdinal(2)) { startCycle(); return; }
      waitForOrdinal(2, function () {
        addTimer(function () {
          if (!clickExerciseOrdinal(1)) { startCycle(); return; }
          waitForOrdinal(1, startCycle);
        }, delays.warmup);
      });
    }, delays.warmup);
  }

  boot();
}

__speexxAutoCycleMain();
