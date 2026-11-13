// Speexx AutoCycle RESUME bookmarklet
//
// HOW TO USE (one-time setup):
//   1. Run the full optimized.js paste in the console once (this stores the
//      cycle's source in localStorage and starts it).
//   2. Create a new bookmark in your browser. Name it "Speexx Resume".
//   3. Copy the SINGLE LINE below (after "MINIFIED:") into the bookmark's
//      URL/address field.
//   4. Whenever a packet boundary triggers a full page reload and the cycle
//      stops, just click the "Speexx Resume" bookmark - it re-injects the
//      stored source from localStorage and the cycle picks right back up on
//      the new packet. No console needed.
//
// This bookmarklet self-heals older broken localStorage copies: if the stored
// source is "function ...(){}();" (a declaration + stray call = SyntaxError),
// it strips the trailing "();" and wraps the whole thing in parens to make a
// valid IIFE "(function ...(){...})();", evals that, and overwrites localStorage
// with the healed form so future clicks work cleanly on the first try.
//
// MINIFIED (paste this as the bookmark URL):
javascript:(function(){try{var k='__speexxAutoCycleSrc';var s=localStorage.getItem(k);if(!s){alert('Speexx Resume: no stored source. Run optimized.js in the console first.');return;}try{(0,eval)(s);}catch(e1){if(!(e1 instanceof SyntaxError)){alert('Speexx Resume error: '+e1);return;}var fixed='('+s.replace(/\(\)\s*;\s*$/,'')+')();';try{(0,eval)(fixed);localStorage.setItem(k,fixed);}catch(e2){alert('Speexx Resume heal error: '+e2);return;}}}catch(e){alert('Speexx Resume error: '+e);}})();
