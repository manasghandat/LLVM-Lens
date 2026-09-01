; ModuleID = 'mba-demo'
; Demo input for the MBAAdd pass: several 8-bit adds get rewritten into a
; mixed boolean-arithmetic expression; the 32-bit add is left untouched.
source_filename = "mba-demo.ll"
target triple = "x86_64-pc-linux-gnu"

define i8 @sum(i8 %a, i8 %b) {
entry:
  %s = add i8 %a, %b
  ret i8 %s
}

define i8 @poly(i8 %x, i8 %y) {
entry:
  %t1 = add i8 %x, %y
  %t2 = add i8 %t1, 5
  %t3 = add i8 %t2, %t2
  ret i8 %t3
}

define i8 @branched(i8 %a, i8 %b, i1 %cond) {
entry:
  br i1 %cond, label %then, label %else

then:
  %t = add i8 %a, %b
  br label %merge

else:
  %e = add i8 %a, 1
  br label %merge

merge:
  %phi = phi i8 [ %t, %then ], [ %e, %else ]
  ret i8 %phi
}

define i32 @wider(i32 %a, i32 %b) {
entry:
  %s = add i32 %a, %b
  ret i32 %s
}
