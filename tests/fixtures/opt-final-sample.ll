; ModuleID = 'sample.ll'
source_filename = "/home/nautilus/repos/LLVM-Lens/tests/fixtures/sample.c"
target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-i128:128-f80:128-n8:16:32:64-S128"
target triple = "x86_64-pc-linux-gnu"

; Function Attrs: nofree noinline norecurse nosync nounwind memory(none) uwtable
define dso_local i32 @main(i32 noundef %0, ptr noundef readnone captures(none) %1) local_unnamed_addr #0 !dbg !10 {
    #dbg_value(i32 %0, !19, !DIExpression(), !20)
    #dbg_value(ptr %1, !21, !DIExpression(), !20)
    #dbg_value(i32 0, !22, !DIExpression(), !20)
    #dbg_value(i32 0, !23, !DIExpression(), !25)
  %3 = icmp sgt i32 %0, 0, !dbg !26
  br i1 %3, label %.lr.ph, label %._crit_edge, !dbg !28

.lr.ph:                                           ; preds = %2, %.lr.ph
  %.08 = phi i32 [ %6, %.lr.ph ], [ 0, %2 ]
  %.067 = phi i32 [ %5, %.lr.ph ], [ 0, %2 ]
    #dbg_value(i32 %.08, !23, !DIExpression(), !25)
    #dbg_value(i32 %.067, !22, !DIExpression(), !20)
  %4 = tail call fastcc i32 @square(i32 noundef %.08), !dbg !29
  %5 = add nsw i32 %4, %.067, !dbg !30
    #dbg_value(i32 %5, !22, !DIExpression(), !20)
  %6 = add nuw nsw i32 %.08, 1, !dbg !31
    #dbg_value(i32 %6, !23, !DIExpression(), !25)
  %exitcond.not = icmp eq i32 %6, %0, !dbg !26
  br i1 %exitcond.not, label %._crit_edge, label %.lr.ph, !dbg !28, !llvm.loop !32

._crit_edge:                                      ; preds = %.lr.ph, %2
  %.06.lcssa = phi i32 [ 0, %2 ], [ %5, %.lr.ph ], !dbg !20
  ret i32 %.06.lcssa, !dbg !35
}

; Function Attrs: mustprogress nofree noinline norecurse nosync nounwind willreturn memory(none) uwtable
define internal fastcc i32 @square(i32 noundef %0) unnamed_addr #1 !dbg !36 {
    #dbg_value(i32 %0, !39, !DIExpression(), !40)
  %2 = mul nsw i32 %0, %0, !dbg !41
  ret i32 %2, !dbg !42
}

attributes #0 = { nofree noinline norecurse nosync nounwind memory(none) uwtable "frame-pointer"="all" "min-legal-vector-width"="0" "no-trapping-math"="true" "stack-protector-buffer-size"="8" "target-cpu"="x86-64" "target-features"="+cmov,+cx8,+fxsr,+mmx,+sse,+sse2,+x87" "tune-cpu"="generic" }
attributes #1 = { mustprogress nofree noinline norecurse nosync nounwind willreturn memory(none) uwtable "frame-pointer"="all" "min-legal-vector-width"="0" "no-trapping-math"="true" "stack-protector-buffer-size"="8" "target-cpu"="x86-64" "target-features"="+cmov,+cx8,+fxsr,+mmx,+sse,+sse2,+x87" "tune-cpu"="generic" }

!llvm.dbg.cu = !{!0}
!llvm.module.flags = !{!2, !3, !4, !5, !6, !7, !8}
!llvm.ident = !{!9}

!0 = distinct !DICompileUnit(language: DW_LANG_C11, file: !1, producer: "Ubuntu clang version 22.1.8 (++20260714014902+ca7933e47d3a-1~exp1~20260714135019.80)", isOptimized: false, runtimeVersion: 0, emissionKind: FullDebug, splitDebugInlining: false, nameTableKind: None)
!1 = !DIFile(filename: "/home/nautilus/repos/LLVM-Lens/tests/fixtures/sample.c", directory: "/tmp/probe", checksumkind: CSK_MD5, checksum: "f5d339b8e0f9cc3b298f640bc1939320")
!2 = !{i32 7, !"Dwarf Version", i32 5}
!3 = !{i32 2, !"Debug Info Version", i32 3}
!4 = !{i32 1, !"wchar_size", i32 4}
!5 = !{i32 8, !"PIC Level", i32 2}
!6 = !{i32 7, !"PIE Level", i32 2}
!7 = !{i32 7, !"uwtable", i32 2}
!8 = !{i32 7, !"frame-pointer", i32 2}
!9 = !{!"Ubuntu clang version 22.1.8 (++20260714014902+ca7933e47d3a-1~exp1~20260714135019.80)"}
!10 = distinct !DISubprogram(name: "main", scope: !11, file: !11, line: 6, type: !12, scopeLine: 6, flags: DIFlagPrototyped, spFlags: DISPFlagDefinition, unit: !0, retainedNodes: !18)
!11 = !DIFile(filename: "/home/nautilus/repos/LLVM-Lens/tests/fixtures/sample.c", directory: "", checksumkind: CSK_MD5, checksum: "f5d339b8e0f9cc3b298f640bc1939320")
!12 = !DISubroutineType(types: !13)
!13 = !{!14, !14, !15}
!14 = !DIBasicType(name: "int", size: 32, encoding: DW_ATE_signed)
!15 = !DIDerivedType(tag: DW_TAG_pointer_type, baseType: !16, size: 64)
!16 = !DIDerivedType(tag: DW_TAG_pointer_type, baseType: !17, size: 64)
!17 = !DIBasicType(name: "char", size: 8, encoding: DW_ATE_signed_char)
!18 = !{}
!19 = !DILocalVariable(name: "argc", arg: 1, scope: !10, file: !11, line: 6, type: !14)
!20 = !DILocation(line: 0, scope: !10)
!21 = !DILocalVariable(name: "argv", arg: 2, scope: !10, file: !11, line: 6, type: !15)
!22 = !DILocalVariable(name: "acc", scope: !10, file: !11, line: 7, type: !14)
!23 = !DILocalVariable(name: "i", scope: !24, file: !11, line: 8, type: !14)
!24 = distinct !DILexicalBlock(scope: !10, file: !11, line: 8, column: 5)
!25 = !DILocation(line: 0, scope: !24)
!26 = !DILocation(line: 8, column: 23, scope: !27)
!27 = distinct !DILexicalBlock(scope: !24, file: !11, line: 8, column: 5)
!28 = !DILocation(line: 8, column: 5, scope: !24)
!29 = !DILocation(line: 9, column: 16, scope: !27)
!30 = !DILocation(line: 9, column: 13, scope: !27)
!31 = !DILocation(line: 8, column: 31, scope: !27)
!32 = distinct !{!32, !28, !33, !34}
!33 = !DILocation(line: 9, column: 24, scope: !24)
!34 = !{!"llvm.loop.mustprogress"}
!35 = !DILocation(line: 10, column: 5, scope: !10)
!36 = distinct !DISubprogram(name: "square", scope: !11, file: !11, line: 2, type: !37, scopeLine: 2, flags: DIFlagPrototyped, spFlags: DISPFlagLocalToUnit | DISPFlagDefinition, unit: !0, retainedNodes: !18)
!37 = !DISubroutineType(types: !38)
!38 = !{!14, !14}
!39 = !DILocalVariable(name: "x", arg: 1, scope: !36, file: !11, line: 2, type: !14)
!40 = !DILocation(line: 0, scope: !36)
!41 = !DILocation(line: 3, column: 14, scope: !36)
!42 = !DILocation(line: 3, column: 5, scope: !36)
