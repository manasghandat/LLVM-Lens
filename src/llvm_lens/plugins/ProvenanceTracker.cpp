//===- ProvenanceTracker.cpp - which pass changed what, and ablate one -----===//
//
// Hooks PassInstrumentation rather than inserting a pass, so it sees any
// pipeline the PassBuilder runs, default<O1/O2/O3> included. It does two jobs:
//
//  1. Log every pass invocation and, after it, every entity it changed.
//     "Changed" means the printed IR differs, which is exactly what
//     -print-changed compares, so both tools agree on which passes did work.
//     (StructuralHash does not: it ignores attributes, `tail`, alignment and
//     globals, and missed a sixth of -print-changed's dumps on default<O2>.)
//
//  2. Skip chosen invocations (-prov-skip=<key>, or PROV_SKIP=<key> in the
//     environment for clang -fpass-plugin), so a second run can show what
//     one invocation caused: the later passes that stop changing without
//     it, and the ones that start.
//
// Written against LLVM 22; builds on 18+.
//
// Build:
//   clang++ -shared -fPIC -O2 ProvenanceTracker.cpp \
//       $(llvm-config --cxxflags) -o libProvTracker.so
// Use:
//   opt -load-pass-plugin=./libProvTracker.so -passes='default<O2>' in.ll \
//       -o /dev/null [-prov-skip=<key>]...
//
// Output (stderr, tab-separated):
//   PROV-RUN   <ord> <key> <class> <pipeline-name> <unit>
//   PROV       <ord> <key> <entity> <created|changed|deleted> <hash>
//   PROV-SKIP  <key> <class> <unit> <ablated|skipped>
//
// <ord> counts invocations exactly as -debug-pass-manager numbers its
// "Running pass:" lines (every pass but the PassManager/PassAdaptor
// containers), so a record maps onto that stream by position. It depends on
// every earlier pass, though, so across two runs an invocation is named by
// its <key> instead:
//   [module]/<class>#<n>             module pass, n-th run of <class>
//   <fn>/<class>#<n>                 function pass, n-th run of <class> on fn
//   <fn>/<adaptor>#<n>/<class>       loop pass, inside that loop adaptor run
//   (<fn>,...)/<class>#<n>           CGSCC pass, n-th run on that SCC
// A loop key covers every loop that adaptor run visited: loops come and go
// with the passes before them, the function's own pipeline does not.
// <entity> is a function name, or [module] for globals and declarations.
//===----------------------------------------------------------------------===//
#include "llvm/ADT/DenseMap.h"
#include "llvm/ADT/SmallPtrSet.h"
#include "llvm/ADT/StringMap.h"
#include "llvm/ADT/StringSet.h"
#include "llvm/Analysis/LazyCallGraph.h"
#include "llvm/Analysis/LoopInfo.h"
#include "llvm/IR/Function.h"
#include "llvm/IR/Module.h"
#include "llvm/IR/PassInstrumentation.h"
#include "llvm/Passes/PassBuilder.h"
// LLVM 22 moved the plugin header; the old path may still resolve to another
// installed LLVM's copy, which declares the wrong plugin API version.
#if __has_include("llvm/Plugins/PassPlugin.h")
#include "llvm/Plugins/PassPlugin.h"
#else
#include "llvm/Passes/PassPlugin.h"
#endif
#include "llvm/Support/CommandLine.h"
#include "llvm/Support/Format.h"
#include "llvm/Support/raw_ostream.h"
#include "llvm/Support/xxhash.h"
#include <cstdlib>
#include <string>

using namespace llvm;

static cl::list<std::string>
    SkipKeys("prov-skip", cl::desc("ProvenanceTracker: skip the pass "
                                   "invocation with this key (repeatable)"));

namespace {

constexpr const char *ModuleEntity = "[module]";

struct Snapshot {
  const Function *F; // compared by address only; never dereferenced after
  std::string Name;
  uint64_t Hash;
};

struct Frame {
  bool Quiet = false; // container: its diff would just repeat its children's
  enum Kind { None, Func, Loop_, SCC_, Mod } K = None;
  unsigned Ord = 0;
  std::string Key;
  const Module *M = nullptr;
  SmallVector<Snapshot, 4> Before;
  uint64_t GlobalsBefore = 0; // module frames: everything but function bodies
};

uint64_t hashText(const std::string &S) {
  return xxh3_64bits(ArrayRef<uint8_t>(
      reinterpret_cast<const uint8_t *>(S.data()), S.size()));
}

uint64_t hashFn(const Function &F) {
  std::string S;
  raw_string_ostream OS(S);
  F.print(OS);
  return hashText(S);
}

// What a module pass can change outside function bodies: globals, aliases,
// declarations (their attributes are inferred), and which functions exist.
uint64_t hashGlobals(const Module &M) {
  std::string S;
  raw_string_ostream OS(S);
  for (const GlobalVariable &G : M.globals())
    G.print(OS), OS << '\n';
  for (const GlobalAlias &A : M.aliases())
    A.print(OS), OS << '\n';
  for (const GlobalIFunc &I : M.ifuncs())
    I.print(OS), OS << '\n';
  for (const Function &F : M) {
    if (F.isDeclaration())
      F.print(OS);
    else
      OS << "define " << F.getName() << '\n';
  }
  return hashText(S);
}

// -debug-pass-manager leaves out the containers whose name, template
// arguments aside, ends in one of these; <ord> must skip exactly the same.
bool isUnprinted(StringRef P) {
  StringRef Prefix = P.substr(0, P.find('<'));
  return Prefix.ends_with("PassManager") || Prefix.ends_with("PassAdaptor");
}

// Containers and bookkeeping passes: logged, but never credited with changes.
bool isQuiet(StringRef P) {
  return isUnprinted(P) || P.contains("Wrapper") || P.contains("Repeated") ||
         P.starts_with("RequireAnalysisPass") ||
         P.starts_with("InvalidateAnalysisPass");
}

class ProvenanceTracker {
  PassInstrumentationCallbacks *PIC = nullptr;
  SmallVector<Frame, 8> Stack; // passes nest: adaptor -> manager -> pass
  unsigned Ord = 0;
  StringMap<unsigned> Counts; // "<unit>/<class>" -> invocations so far
  StringSet<> Skip;

  // The key ShouldRunOptionalPass computed, for the Before* callback that
  // follows it. Required passes get no ShouldRun, so they compute their own.
  std::string PendingPass, PendingKey, PendingUnit;
  bool Pending = false;

  StringRef shortName(StringRef ClassName) const {
    StringRef N = PIC->getPassNameForClassName(ClassName);
    return N.empty() ? ClassName : N;
  }

  // The innermost enclosing invocation that ran on a function, which for a
  // loop pass is its loop adaptor.
  const Frame *enclosingFunctionFrame() const {
    for (const Frame &Fr : reverse(Stack))
      if (Fr.K == Frame::Func)
        return &Fr;
    return nullptr;
  }

  std::string counted(const std::string &Unit, StringRef P) {
    std::string Base = Unit + "/" + P.str();
    return Base + "#" + std::to_string(++Counts[Base]);
  }

  // Name this invocation, and describe its IR unit for the log. Called
  // exactly once per invocation, skipped or not, so counts stay aligned
  // across runs that skip different things.
  void name(StringRef P, Any IR, std::string &Key, std::string &Unit) {
    if (const auto *F = llvm::any_cast<const Function *>(&IR)) {
      Unit = ("function @" + (*F)->getName()).str();
      Key = counted((*F)->getName().str(), P);
    } else if (const auto *L = llvm::any_cast<const Loop *>(&IR)) {
      const BasicBlock *H = (*L)->getHeader();
      const Function *F = H->getParent();
      Unit = ("loop %" + (H->hasName() ? H->getName() : "<unnamed>") +
              " in @" + F->getName())
                 .str();
      const Frame *Outer = enclosingFunctionFrame();
      Key = (Outer ? Outer->Key : F->getName().str()) + "/" + P.str();
    } else if (const auto *C = llvm::any_cast<const LazyCallGraph::SCC *>(&IR)) {
      std::string Members;
      for (const LazyCallGraph::Node &N : **C)
        Members += (Members.empty() ? "" : ",") + N.getFunction().getName().str();
      Unit = "cgscc (" + Members + ")";
      Key = counted("(" + Members + ")", P);
    } else if (llvm::any_cast<const Module *>(&IR)) {
      Unit = "module";
      Key = counted(ModuleEntity, P);
    } else {
      Unit = "?";
      Key = counted("?", P);
    }
  }

  void take(StringRef P, Any IR, std::string &Key, std::string &Unit) {
    if (Pending && PendingPass == P) {
      Key = std::move(PendingKey);
      Unit = std::move(PendingUnit);
    } else {
      name(P, IR, Key, Unit);
    }
    Pending = false;
  }

  static void snapshot(Any IR, Frame &Fr) {
    SmallVector<const Function *, 4> Fs;
    if (const auto *F = llvm::any_cast<const Function *>(&IR)) {
      Fr.K = Frame::Func;
      Fr.M = (*F)->getParent();
      Fs.push_back(*F);
    } else if (const auto *L = llvm::any_cast<const Loop *>(&IR)) {
      Fr.K = Frame::Loop_;
      Fs.push_back((*L)->getHeader()->getParent());
      Fr.M = Fs.back()->getParent();
    } else if (const auto *C = llvm::any_cast<const LazyCallGraph::SCC *>(&IR)) {
      Fr.K = Frame::SCC_;
      for (const LazyCallGraph::Node &N : **C)
        Fs.push_back(&N.getFunction());
      Fr.M = Fs.empty() ? nullptr : Fs.front()->getParent();
    } else if (const auto *M = llvm::any_cast<const Module *>(&IR)) {
      Fr.K = Frame::Mod;
      Fr.M = *M;
      for (const Function &F : **M)
        if (!F.isDeclaration())
          Fs.push_back(&F);
      Fr.GlobalsBefore = hashGlobals(**M);
    }
    for (const Function *F : Fs)
      Fr.Before.push_back({F, F->getName().str(), hashFn(*F)});
  }

  void report(const Frame &Fr, StringRef Entity, StringRef What, uint64_t H) {
    errs() << "PROV\t" << Fr.Ord << '\t' << Fr.Key << '\t' << Entity << '\t'
           << What << '\t' << format_hex_no_prefix(H, 16) << '\n';
  }

  void finish(Frame Fr) {
    if (Fr.Quiet || Fr.K == Frame::None || !Fr.M)
      return;
    // A pass may delete functions (and the allocator may hand a freed
    // Function's address to a new one), so an old pointer is only ever
    // compared against the module's live functions, never dereferenced.
    SmallPtrSet<const Function *, 32> Live;
    for (const Function &F : *Fr.M)
      if (!F.isDeclaration())
        Live.insert(&F);

    SmallPtrSet<const Function *, 32> Before;
    for (const Snapshot &S : Fr.Before) {
      Before.insert(S.F);
      if (!Live.count(S.F)) {
        report(Fr, S.Name, "deleted", 0);
        continue;
      }
      uint64_t H = hashFn(*S.F);
      if (H != S.Hash)
        report(Fr, S.F->getName(), "changed", H);
    }
    if (Fr.K == Frame::Mod) {
      for (const Function &F : *Fr.M)
        if (!F.isDeclaration() && !Before.count(&F))
          report(Fr, F.getName(), "created", hashFn(F));
      uint64_t G = hashGlobals(*Fr.M);
      if (G != Fr.GlobalsBefore)
        report(Fr, ModuleEntity, "changed", G);
    }
  }

public:
  void registerCallbacks(PassInstrumentationCallbacks &C) {
    PIC = &C;
    for (const std::string &K : SkipKeys)
      Skip.insert(K);
    if (const char *Env = std::getenv("PROV_SKIP")) {
      SmallVector<StringRef, 4> Keys;
      StringRef(Env).split(Keys, '\n', -1, /*KeepEmpty=*/false);
      for (StringRef K : Keys)
        Skip.insert(K.trim());
    }

    // Only optional passes get here; this is where one of them is ablated.
    C.registerShouldRunOptionalPassCallback([this](StringRef P, Any IR) {
      PendingPass = P.str();
      name(P, IR, PendingKey, PendingUnit);
      Pending = true;
      return !Skip.count(PendingKey);
    });

    C.registerBeforeSkippedPassCallback([this](StringRef P, Any IR) {
      std::string Key, Unit;
      take(P, IR, Key, Unit);
      errs() << "PROV-SKIP\t" << Key << '\t' << P << '\t' << Unit << '\t'
             << (Skip.count(Key) ? "ablated" : "skipped") << '\n';
    });

    C.registerBeforeNonSkippedPassCallback([this](StringRef P, Any IR) {
      Frame Fr;
      std::string Unit;
      take(P, IR, Fr.Key, Unit);
      if (!isUnprinted(P))
        Fr.Ord = ++Ord;
      Fr.Quiet = isQuiet(P);
      if (Fr.Ord)
        errs() << "PROV-RUN\t" << Fr.Ord << '\t' << Fr.Key << '\t' << P << '\t'
               << shortName(P) << '\t' << Unit << '\n';
      if (Fr.Quiet) {
        // Still record its unit kind: a loop pass's key names its adaptor.
        if (llvm::any_cast<const Function *>(&IR))
          Fr.K = Frame::Func;
      } else {
        snapshot(IR, Fr);
      }
      Stack.push_back(std::move(Fr));
    });

    C.registerAfterPassCallback(
        [this](StringRef, Any, const PreservedAnalyses &) {
          finish(Stack.pop_back_val());
        });

    // The IR unit is gone (a fully unrolled loop, a merged SCC); the
    // functions that held it are compared against the module instead.
    C.registerAfterPassInvalidatedCallback(
        [this](StringRef, const PreservedAnalyses &) {
          finish(Stack.pop_back_val());
        });
  }
};

} // namespace

extern "C" LLVM_ATTRIBUTE_WEAK PassPluginLibraryInfo llvmGetPassPluginInfo() {
  return {LLVM_PLUGIN_API_VERSION, "ProvenanceTracker", "1.0",
          [](PassBuilder &PB) {
            PassInstrumentationCallbacks *PIC =
                PB.getPassInstrumentationCallbacks();
            if (!PIC) {
              errs() << "ProvenanceTracker: PassBuilder has no "
                        "instrumentation callbacks; nothing will be logged\n";
              return;
            }
            // One tracker per PassBuilder; intentionally leaked (lives for
            // the whole compilation).
            (new ProvenanceTracker())->registerCallbacks(*PIC);
          }};
}
