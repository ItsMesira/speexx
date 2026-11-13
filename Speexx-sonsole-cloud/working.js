CourseWare.CourseExercises.CourseExercisesControlsView =
  Backbone.Speexx.HandlebarsView.extend({
    templateName: "cw-language-course-controls",
    className: "exercise-controls",
    initialize: function (e) {
      var t = (this.exerciseView = e.exerciseView),
        n = t.model;
      this.firstTime = !0;
      this.solutionShown = !1;
      this.modified = !1;
      this.dialogEnded = !1;

      this.listenTo(t, "modified", function () {
        this.modified || ((this.modified = !0), this.render());
      });
      this.listenToOnce(t, "dialog:ended", function () {
        ((this.dialogEnded = !0), this.render());
      });
      this.listenTo(t, "static:added static:removed", this.render);
      this.listenTo(n, "sync", function (e, t, n) {
        var r = !!this.silent;
        this.silent = !!n.silent;
        (!this.silent || !r) && this.render();
      });
      this.listenTo(CourseWare.Language, "change:textpool", this.render);
      this.on("render:after", function () {
        this.$(".btn.btn-link[title]").tooltip();
      });

      window.entryp = this;

      // ✅ Start the auto-cycle immediately after this view is ready
      this._startAutoCycle();
    },

    _startAutoCycle: function () {
      var self = this;

      function runCycle() {
        // Guard: skip if view is gone
        if (!self || self._isDestroyed) return;

        self.trigger("solve");

        setTimeout(function () {
          if (!self || self._isDestroyed) return;
          self.trigger("correct");
        }, 5000); // was 10000, trimmed since solve+correct don't need 10s gap

        setTimeout(function () {
          if (!self || self._isDestroyed) return;
          var $next = self.$(".next");
          if ($next.length) {
            $next.click();
          } else {
            $(".next").click(); // fallback
          }

          // Schedule next cycle AFTER next is clicked, not on a fixed interval
          setTimeout(runCycle, 3000); // small buffer after clicking next
        }, 10000); // was 15000
      }

      // ✅ Small delay so the view finishes rendering before first cycle
      setTimeout(runCycle, 500);
    },

    templateModel: function () {
      var e = this.exerciseView,
        t = e.model,
        n = this.silent ? null : t.get("result"),
        r = t.get("type").pronunciation,
        i = !e.static && t.get("type").hasResult && !r,
        s = e.static || !t.get("type").hasResult,
        o = !this.solutionShown && i && !this.firstTime && n !== 100;
      return {
        firstTime: this.firstTime,
        hasCorrect: i,
        hasSolution: o,
        hideResult: s,
        dialogEnded: this.dialogEnded,
        modified: this.modified,
        solutionShown: this.solutionShown,
        result: n,
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
        (e.originalEvent.charCode === 13 || e.originalEvent.charCode === 32) &&
          this.trigger("nextKeyboard");
      },
      "click .btn.solution": function () {
        this.render();
        this.trigger("solve");
      },
    },
  });
