// JavaScript for sticky menu
window.onscroll = function() {makeSticky()};

var header = document.getElementById("header");
var sticky = menu.offsetTop;

function makeSticky() {
  if (window.pageYOffset >= sticky) {
    header.classList.add("sticky");
  } else {
    header.classList.remove("sticky");
  }
}
