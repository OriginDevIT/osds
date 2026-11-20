// Prefill path_segment from key until the operator edits path_segment.
document.addEventListener("DOMContentLoaded", function () {
  var seg = document.querySelector("input[data-follows]");
  if (!seg) return;
  var key = document.getElementById(seg.dataset.follows);
  if (!key) return;
  var touched = seg.value !== "" && seg.value !== key.value;
  seg.addEventListener("input", function () { touched = seg.value !== "" && seg.value !== key.value; });
  key.addEventListener("input", function () { if (!touched) seg.value = key.value; });
  if (!touched && seg.value === "") seg.value = key.value;
});
