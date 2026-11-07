// ===============================================================================
//     CHAPTER 9: THE LOW-LEVEL RUNTIME ENGINE & MEMORY SUBSYSTEM
// ===============================================================================
//
// 1. THE ROLE OF A COMPILER RUNTIME
// ---------------------------------
// A native compiler rarely emits raw machine instructions for everything. While
// simple operations (scalar integer addition, bitwise shifts, local branches)
// translate directly into 1-3 machine opcodes, complex capabilities—string slicing,
// array broadcasting, Unicode transcoding, error stack unwinding, and dynamic memory
// management—are delegated to a minimal runtime library linked into every executable.
//
// 2. 64-BIT VIRTUAL MEMORY AS AN ALLOCATION STRATEGY
// --------------------------------------------------
// Traditional runtimes rely on general-purpose heaps (such as Doug Lea's dlmalloc or
// jemalloc) with `malloc()` and `free()`. However, general-purpose heaps suffer from:
//   * Non-deterministic fragmentation.
//   * Per-allocation metadata overhead (chunk headers, free-list pointers).
//   * Cache misses caused by scattered pointer-chasing.
//
// Ella leverages modern 64-bit Linux architecture (docs/memory-model.md §5, §14):
//   * Address space is abundant (128 TiB of user-space virtual addresses).
//   * The runtime reserves up to 32 TiB upfront with `mmap(..., MAP_NORESERVE)`.
//     No physical RAM is committed until an address is actually written to.
//   * Scope arenas allocate via ultra-fast *bump allocation*:
//         top = top + round16(size);
//   * Resetting an arena at scope or loop iteration exit is O(1):
//         top = start;
//   * Pages beyond 16 MiB are lazily returned to the Linux kernel via
//     `madvise(MADV_DONTNEED)`.
//
// 3. CPU CACHE CONFLICT MITIGATION: ARENA OFFSET STAGGERING
// ---------------------------------------------------------
// Modern CPU caches (L1/L2) are set-associative (typically 8-way or 16-way). Addresses
// that differ by exact multiples of large powers of two map to the identical cache set.
//
// When Ella's alternating loop arenas (`@k` and `@carryk`) take turns carrying values,
// if both arenas started at exact power-of-two boundaries, corresponding objects would
// land in identical cache sets, causing severe cache conflict thrashing and slowing down
// tight numerical loops by over 10x!
// The runtime eliminates this by offsetting each depth's arena base by a non-power-of-two
// prime multiple (`#define STAGGER ((size_t)65 << 12)`).
//
// 4. GENERATIONAL POOL HANDLES: MEMORY SAFETY WITHOUT GC
// ------------------------------------------------------
// Objects that outlive lexical stack scopes live in a `Pool[T]`.
// To guarantee complete safety against Use-After-Free (UAF) and cross-pool tampering:
//   * A `Handle[T]` is packed into a single 64-bit word:
//       [ 24 bits: Pool ID | 20 bits: Generation | 20 bits: Slot Index ]
//   * Dereferencing a handle performs an O(1) generation check against the slot table.
//     If the slot has been freed and reused, the generation mismatch triggers a safe
//     trap instead of reading corrupted memory.
//
// The Ella runtime: support code linked into every native Ella program.
//
// The code generator (ella/codegen.py) turns IR into x64 assembly. Simple
// operations, like adding two i64s, become a few instructions. Anything
// bigger, like joining strings or printing a list, calls a function here.
// This runtime must behave exactly like ella/operators.py and ella/values.py,
// so that native programs print the same output and the same errors as the
// interpreters.
//
// All values are 64-bit words. Numbers and bools are the word itself (an f64
// is its raw bits); everything else is a pointer to one of the structs
// below, which live in arenas (see "Arenas" below, and docs/memory-model.md).

#include <errno.h>
#include <fcntl.h>
#include <immintrin.h>
#include <inttypes.h>
#include <math.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/types.h>
#include <unistd.h>

typedef int64_t word;

static int unwinding;
static void unwind_arenas(void);

#define UNUSED(x) (void)(x)

// A string, as Unicode code points, so indexing and len() are simple. Its
// characters follow the header, unless it is a *view* (docs/memory-model.md
// §18): a slice whose characters are part of another string's.
typedef struct {
    word len;
    uint32_t *chars;
} Str;

// A list: a length, and a pointer to that many words. Its items follow the
// header, with room for `cap` of them, unless it is a view of another list's
// items, with `cap` 0.
typedef struct {
    word len;
    word cap;
    word *items;
} List;

// An array (docs/grammar.md §10, "Arrays"): its number of items, its number
// of dimensions, and where its items are, in the places a list has its length
// and items, so that the code generator's code for a list's length and items
// works on an array's too; then the kind of its items (I64_ITEMS, F64_ITEMS,
// F32_ITEMS or I32_ITEMS: an f32 or i32 takes 4 bytes, the others 8), and the size of each
// dimension. Its items follow, in row-major order, at a 32-byte boundary, as
// AVX2 likes them.
typedef struct {
    word len;
    word ndim;
    void *items;
    word kind;
    word dims[];
} Array;

enum { I64_ITEMS, F64_ITEMS, F32_ITEMS, I32_ITEMS };

// A tuple: a length and that many words, in the object. (`cap` is its length.)
typedef struct {
    word len;
    word cap;
    word items[];
} Tuple;

// `start..stop`, with `stop` excluded. `stop` is 128 bits wide because
// `0..=9223372036854775807` stops one past the largest i64.
typedef struct {
    word start;
    __int128 stop;
} Range;

// An enum value: which variant (its index in the enum), and its fields.
typedef struct {
    word tag;
    word count;
    word fields[];
} Variant;

// A function value: the code to run and the values it captured, with their
// types (a tuple type descriptor), which copying a closure needs.
typedef struct Type Type;
typedef struct {
    void *code;
    word count;
    const Type *types;
    word captured[];
} Closure;

// A generator (docs/memory-model.md §17): the code of its body, a generator
// function, which `resume` calls as it calls a closure's; where that code
// carries on (after the `yield` it last stopped at); a slot arena for each
// of its scope levels; and its frame, `size` bytes holding its values,
// which the code addresses from the end (in r12; see ella.codegen).
typedef struct {
    void *code;
    void *resume;
    word levels;
    word size;
    word depths[];
} Gen;

static size_t gen_size(const Gen *g) { return sizeof(Gen) + (size_t)g->levels * sizeof(word) + (size_t)g->size; }

// Type descriptors, emitted as static data by the code generator. Equality
// and the text form of a list of enums of floats need to know all of that.
enum Kind { K_UNIT, K_I64, K_F64, K_BOOL, K_STR, K_RANGE, K_LIST, K_TUPLE, K_FUNCTION, K_ENUM, K_POOL, K_HANDLE, K_CODE, K_ARENA, K_REF, K_GEN, K_ARRAY, K_F32, K_I32 };

struct Type {
    word kind;
    word count;  // tuple items or enum variants
    // list and array: parts[0] is the element type; tuple: the item types;
    // enum: VariantType descriptors.
    const void *parts[];
};

// A variant (or struct). A nocopy struct may have a finalizer, a closure run
// on each value when it dies. `error` marks the built-in Error, whose fields
// are its message, its cause (an Optional) and where it was made: equality and
// the text form leave that out, and show the cause without its Some. `text`,
// `eq` and `hash` are the closures of its type's methods with those names, if
// it has them: `print` calls `text` for its text form, `==` (inside other
// values too) and `in` call `eq`, and `hash` calls `hash`.
typedef struct {
    const char *name;
    word count;
    const Closure *finalize;
    word error;
    const Closure *text, *eq, *hash;
    const Type *fields[];
} VariantType;

// How many Ella calls are in progress, for the stack overflow check. The
// top-level code itself counts as one, as in the interpreters.
word ella_depth = 1;

// Errors

// The files of the program's modules, made by the code generator. A line
// passed to the runtime has the number of its file above its low 32 bits;
// file 0 is the entry file, whose errors don't name it.
extern const char *const ella_files[];

// Stack traces. Each call puts its site, made by the code generator, in
// ella_trace at the call depth before it counts the call: where it is (a
// line and column as passed to the runtime), and the function it is in ("" for
// the top-level code). The call of main has no site (NULL), and a finalizer's
// call is at 0:0. The calls in progress are those at depths 1 to ella_depth - 1.
typedef struct {
    word line, column;
    const char *function;
} Site;

extern const Site *ella_trace[];  // in the generated code, by depth
const Site ella_finalizer_site = {0, 0, ""};
const Site ella_text_site = {0, 1, ""};  // a `text` method's call, for print
const Site ella_eq_site = {0, 2, ""};    // an `eq` method's, for == and in
const Site ella_hash_site = {0, 3, ""};  // a `hash` method's, for hash

// A stack trace of more calls than twice this shows only this many at each
// end (TRACE_ENDS in ella.lexer).
enum { TRACE_ENDS = 10 };

// A place in the source: "line:column", or "file:line:column" in a module
// other than the entry file.
static void print_place(word line, word column) {
    word file = line >> 32;
    if (file) fprintf(stderr, "%s:", ella_files[file]);
    fprintf(stderr, "%" PRId64 ":%" PRId64, line & 0xffffffff, column);
}

// The innermost call in progress below `depth` that has a site, or 0.
static word site_below(word depth) {
    while (--depth >= 1 && ella_trace[depth] == NULL) {
    }
    return depth;
}

// The stack trace of the calls in progress below `depth`, innermost first: a
// line each, "  called at line:column in function", as EllaError.format.
static void print_trace(word depth) {
    word count = 0;
    for (word d = site_below(depth); d >= 1; d = site_below(d)) count++;
    word shown = 0;
    for (word d = site_below(depth); d >= 1; d = site_below(d), shown++) {
        if (count > 2 * TRACE_ENDS && shown >= TRACE_ENDS && shown < count - TRACE_ENDS) {
            if (shown == TRACE_ENDS) fprintf(stderr, "  ... %" PRId64 " more calls\n", count - 2 * TRACE_ENDS);
            continue;
        }
        const Site *site = ella_trace[d];
        if (site == &ella_finalizer_site) {
            fputs("  called to finalize a value\n", stderr);
            continue;
        }
        if (site == &ella_text_site) {
            fputs("  called to make a value's text\n", stderr);
            continue;
        }
        if (site == &ella_eq_site) {
            fputs("  called to compare two values\n", stderr);
            continue;
        }
        if (site == &ella_hash_site) {
            fputs("  called to hash a value\n", stderr);
            continue;
        }
        fputs("  called at ", stderr);
        print_place(site->line, site->column);
        if (site->function[0]) fprintf(stderr, " in %s", site->function);
        fputc('\n', stderr);
    }
}

// Start a run-time error, after everything the program printed so far:
// "line:column: ". Returns the depth whose calls in progress its stack trace
// shows. An error at 0:0 is in a function made for a library function used as
// a value, and is reported at its call, as in EllaError.traced.
static word start_error(word line, word column) {
    fflush(stdout);
    word depth = ella_depth;
    if (line == 0 && column == 0 && site_below(depth) >= 1) {
        depth = site_below(depth);
        line = ella_trace[depth]->line, column = ella_trace[depth]->column;
    }
    print_place(line, column);
    fputs(": ", stderr);
    return depth;
}

// End a run-time error after its message: the stack trace, then stop.
_Noreturn static void end_error(word depth) {
    fputc('\n', stderr);
    print_trace(depth);
    if (!unwinding) {
        unwinding = 1;
        unwind_arenas();
    }
    exit(1);
}

// Stop the program with a run-time error: "line:column: message", then the stack trace.
_Noreturn static void fail(word line, word column, const char *format, ...) {
    word depth = start_error(line, column);
    va_list args;
    va_start(args, format);
    vfprintf(stderr, format, args);
    va_end(args);
    end_error(depth);
}

_Noreturn void ella_error(word line, word column, const char *message) { fail(line, column, "%s", message); }

_Noreturn void ella_overflow(word a, word b, const char *op, word line, word column) {
    fail(line, column, "i64 overflow: %" PRId64 " %s %" PRId64 " doesn't fit in 64 bits", a, op, b);
}

// i32 arithmetic that doesn't fit in 32 bits. (An i32 is kept sign-extended in its word.)
_Noreturn void ella_overflow32(word a, word b, const char *op, word line, word column) {
    fail(line, column, "i32 overflow: %" PRId64 " %s %" PRId64 " doesn't fit in 32 bits", a, op, b);
}

_Noreturn void ella_overflow32_neg(word a, word line, word column) {
    fail(line, column, "i32 overflow: -(%" PRId64 ") doesn't fit in 32 bits", a);
}

_Noreturn void ella_overflow_neg(word a, word line, word column) {
    fail(line, column, "i64 overflow: -(%" PRId64 ") doesn't fit in 64 bits", a);
}

_Noreturn void ella_stack_overflow(word limit, word line, word column) {
    fail(line, column, "stack overflow: more than %" PRId64 " nested calls", limit);
}

_Noreturn void ella_unset_global(const char *name, word line, word column) {
    fail(line, column, "'%s' is used before its 'let' has run", name);
}

_Noreturn static void out_of_memory(const char *what) {
    fflush(stdout);
    fprintf(stderr, "out of memory: %s\n", what);
    exit(2);
}

// Arenas
//
// Every object lives in an arena, numbered by depth. Depth 0 is the global
// arena, freed when the program ends. Each Ella call takes the next few
// depths for its scope levels (its body, and one more for each loop nested
// in it) and frees them when it returns; a loop frees its level after each
// iteration. The generated code passes the depth to every function here
// that makes an object.
//
// A pool's values each live in an arena of their own, a *slot arena*,
// numbered from SLOT_DEPTH up, which is freed when the value is. An Ella
// `Arena` (a sub-arena) has one slot arena for all its values.
//
// All the arenas are parts of one range of address space, reserved when the
// program starts: up to 1 GiB for each scope arena (there is room for two
// scope levels per nested call, and some more, in at most SCOPE_SPACE), then
// SLOT_SIZE for each slot arena. So an arena never moves, and the arena an object is in
// can be worked out from its address. Making an object just bumps the
// arena's `top` pointer; the system gives the memory pages as they are first
// used, and freeing an arena sets `top` back to the start.

#define SCOPE_SPACE ((size_t)32 << 40)  // 32 TiB for all the scope arenas
#define SLOT_ARENAS ((word)1 << 20)
#define SLOT_SIZE ((size_t)16 << 20)  // 16 MiB per slot arena
#define SLOT_DEPTH ((word)1 << 20)
#define REF_GENERATIONS ((word)1 << 24)
#define HANDLE_SLOTS ((word)1 << 20)
#define HANDLE_GENERATIONS ((word)1 << 20)
#define POOL_IDS ((word)1 << 24)
#define KEEP_SCOPE ((size_t)16 << 20)  // when freeing, give memory beyond this back to the OS
#define KEEP_SLOT ((size_t)64 << 10)

// A value with a finalizer, in the arena it lives in: a nocopy struct with
// its finalizer, a pool (with no closure), which frees its values, a
// sub-arena (with SUB_ARENA), which frees its slot arena, or a generator
// (with GEN), which frees its levels.
typedef struct {
    word object;
    const Closure *finalize;
} Final;

#define SUB_ARENA ((const Closure *)1)
#define GEN ((const Closure *)2)

typedef struct {
    char *top;
    Final *finals;
    word count, capacity;
    word generation;  // goes up each time it is freed (only refs into sub-arenas use it)
    size_t last_used;  // how much it held when it was last freed
    size_t touched;  // how much of it may be memory the system has given it
} Arena;

static char *space;  // all the arenas' address space
static word scope_count;  // how many scope arenas there are
static size_t scope_size;  // and the address space of each
// Each depth's arena starts a little later than a multiple of scope_size: by
// `stagger` bytes for each depth, in turn for 16 depths. Arenas whose objects
// are at the same offsets from starts a power of two apart make the CPU's
// caches slow down by ten times or more for a loop that carries an array from
// one iteration to the next, in two arenas that take turns.
#define STAGGER ((size_t)65 << 12)
#define STAGGERS 16
static size_t stagger;
static Arena *scope_arenas;
static Arena *slot_arenas;
static word *free_slots, free_slot_count, slot_arenas_used;
static word pool_ids_used;

// The first depth not taken by a call in progress (depth 0 is global).
word ella_next_depth = 1;

// With ELLA_POISON set, freed memory is overwritten, so that reading an
// object after its arena is freed (a bug in the compiler) shows up as
// nonsense rather than going unnoticed. The tests set it.
static int poison;

// With ELLA_ARENA_STATS set, the program reports the most memory its arenas
// ever held at once. ELLA_RUNTIME_STATS reports all memory statistics.
static size_t in_use, peak;
static size_t copied_bytes;

static size_t round16(size_t size) { return (size + 15) & ~(size_t)15; }

extern const word ella_max_depth;  // in the generated code: how deeply calls may nest

static char *arena_start(word depth);

static void reserve(void) {
    scope_count = 2 * ella_max_depth + 1024;
    if (scope_count >= SLOT_DEPTH) out_of_memory("can't reserve address space");
    for (scope_size = (size_t)1 << 30; (size_t)scope_count * scope_size > SCOPE_SPACE;) scope_size /= 2;
    size_t size = (size_t)scope_count * scope_size + (size_t)SLOT_ARENAS * SLOT_SIZE;
    space = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS | MAP_NORESERVE, -1, 0);
    scope_arenas = calloc((size_t)scope_count, sizeof(Arena));
    slot_arenas = calloc((size_t)SLOT_ARENAS, sizeof(Arena));
    free_slots = malloc((size_t)SLOT_ARENAS * sizeof(word));
    if (space == MAP_FAILED || scope_arenas == NULL || slot_arenas == NULL || free_slots == NULL)
        out_of_memory("can't reserve address space");
    for (stagger = STAGGER; STAGGERS * stagger > scope_size / 2;) stagger /= 2;
    stagger &= ~(size_t)15;
    for (word i = 0; i < scope_count; i++) scope_arenas[i].top = arena_start(i);
}

static char *arena_start(word depth) {
    if (depth >= SLOT_DEPTH) return space + scope_count * scope_size + (size_t)(depth - SLOT_DEPTH) * SLOT_SIZE;
    return space + depth * scope_size + (size_t)(depth % STAGGERS) * stagger;
}

// How much an arena can hold.
static size_t arena_limit(word depth) {
    return depth >= SLOT_DEPTH ? SLOT_SIZE : scope_size - (size_t)(depth % STAGGERS) * stagger;
}

static Arena *arena(word depth) {
    if (depth >= SLOT_DEPTH) return &slot_arenas[depth - SLOT_DEPTH];
    if (depth < 0 || depth >= scope_count) out_of_memory("too many nested scopes");
    return &scope_arenas[depth];
}

// The arena an object is in, from its address: -1 for static data.
word ella_region_of(word object) {
    char *p = (char *)object;
    char *slots = space + scope_count * scope_size;
    if (p >= space && p < slots) return (word)((size_t)(p - space) / scope_size);
    if (p >= slots && p < slots + (size_t)SLOT_ARENAS * SLOT_SIZE) return SLOT_DEPTH + (word)((size_t)(p - slots) / SLOT_SIZE);
    return -1;
}

static void *alloc(size_t size, word depth) {
    Arena *a = arena(depth);
    char *object = a->top;
    if ((size_t)(object + round16(size) - arena_start(depth)) > arena_limit(depth)) out_of_memory("an arena is full");
    a->top = object + round16(size);
    in_use += round16(size);
    if (in_use > peak) peak = in_use;
    return object;
}

// Remember a value with a finalizer, made in the arena at `depth`.
void ella_register(word object, const Closure *finalize, word depth) {
    Arena *a = arena(depth);
    if (a->count == a->capacity) {
        a->capacity = a->capacity ? 2 * a->capacity : 8;
        a->finals = realloc(a->finals, (size_t)a->capacity * sizeof(Final));
        if (a->finals == NULL) out_of_memory("can't grow a finalization list");
    }
    a->finals[a->count++] = (Final){object, finalize};
}

// Forget a value's finalizer, when the value moves to another arena.
static void unregister(word object) {
    word depth = ella_region_of(object);
    if (depth < 0) return;
    Arena *a = arena(depth);
    for (word i = a->count - 1; i >= 0; i--) {
        if (a->finals[i].object == object) {
            memmove(&a->finals[i], &a->finals[i + 1], (size_t)(a->count - i - 1) * sizeof(Final));
            a->count--;
            return;
        }
    }
}

// In the generated code: call an Ella closure, with its result at `depth`, counting the call at `site`.
extern word ella_call1(const Closure *closure, word argument, word depth, const Site *site);
extern word ella_call2(const Closure *closure, word a, word b, word depth, const Site *site);
static void free_pool(word pool);
static void free_sub_arena(word arena);
static void free_gen(word gen);

// Run the finalizers of the values in an arena, newest first.
static void finalize_all(Arena *a) {
    while (a->count > 0) {
        Final final = a->finals[--a->count];
        if (final.finalize == NULL) {
            free_pool(final.object);
        } else if (final.finalize == SUB_ARENA) {
            free_sub_arena(final.object);
        } else if (final.finalize == GEN) {
            free_gen(final.object);
        } else {
            ella_call1(final.finalize, final.object, 0, &ella_finalizer_site);
        }
    }
}

static void free_arena(word depth) {
    Arena *a = arena(depth);
    finalize_all(a);  // first, while the values are still there
    char *start = arena_start(depth);
    size_t used = (size_t)(a->top - start);
    in_use -= used;
    if (poison) memset(start, 0xA5, used);
    a->top = start;
    a->generation++;
    // Memory well beyond what the arena usually needs goes back to the OS.
    // It reads as zeros if it is used again. A scope arena keeps what it held
    // when it was last freed, so that a loop that makes big values (arrays)
    // in each iteration doesn't have the system give it the same pages again
    // each time; once it holds less two times running, the rest goes back.
    size_t keep = depth >= SLOT_DEPTH ? KEEP_SLOT : KEEP_SCOPE;
    if (depth < SLOT_DEPTH && a->last_used > keep) keep = a->last_used;
    size_t touched = used > a->touched ? used : a->touched;
    if (touched > keep) {
        madvise(start + keep, touched - keep, MADV_DONTNEED);
        touched = keep;
    }
    a->touched = touched;
    a->last_used = used;
}

// Free the arenas at depths end - 1, ..., first: a loop's levels when an
// iteration ends.
void ella_reset(word first, word end) {
    for (word depth = end - 1; depth >= first; depth--) free_arena(depth);
}

static void unwind_arenas(void) { ella_reset(0, ella_next_depth); }

// A loop that carries values (docs/memory-model.md §6) ends an iteration:
// free the levels of the loops inside it (first .. end - 1), run the
// finalizers of the iteration's values (in arena `current`; the values the
// loop keeps can't have any), and free `carried`, the values it kept from
// the iteration before. The generated code then swaps the two depths: the
// iteration's arena holds what the loop carries, and the freed one is the
// next iteration's.
void ella_swap(word first, word end, word current, word carried) {
    ella_reset(first, end);
    finalize_all(arena(current));
    free_arena(carried);
}

// A call starts: take `levels` depths, and return the first.
word ella_enter(word levels) {
    word base = ella_next_depth;
    if (base + levels > scope_count) out_of_memory("too many nested scopes");
    ella_next_depth = base + levels;
    return base;
}

// A call returns: free its levels, and give back their depths.
void ella_leave(word base) {
    ella_reset(base, ella_next_depth);
    ella_next_depth = base;
}

static word new_slot_arena(void) {
    if (free_slot_count > 0) return free_slots[--free_slot_count];
    if (slot_arenas_used == SLOT_ARENAS) out_of_memory("too many values in pools");
    word depth = SLOT_DEPTH + slot_arenas_used++;
    arena(depth)->top = arena_start(depth);
    return depth;
}

static void free_slot_arena(word depth) {
    free_arena(depth);
    // Ref generations are encoded in 24 bits. Retire an arena before its
    // generation could wrap and make an old ref valid again.
    if (arena(depth)->generation >= REF_GENERATIONS) return;
    free_slots[free_slot_count++] = depth;
}

// Integer arithmetic. `+`, `-` and `*` are done by generated code directly;
// these need more care.

word ella_floordiv(word a, word b, word line, word column) {
    if (b == 0) fail(line, column, "integer division by zero");
    if (a == INT64_MIN && b == -1) ella_overflow(a, b, "//", line, column);
    // C rounds toward zero; Ella rounds toward negative infinity.
    word q = a / b, r = a % b;
    if (r != 0 && ((r < 0) != (b < 0))) q -= 1;
    return q;
}

word ella_mod(word a, word b, word line, word column) {
    if (b == 0) fail(line, column, "integer division by zero");
    if (b == -1) return 0;  // INT64_MIN % -1 would crash the hardware divide
    word r = a % b;
    // The result has the sign of b, matching `//`.
    if (r != 0 && ((r < 0) != (b < 0))) r += b;
    return r;
}

word ella_floordiv(word a, word b, word line, word column);
word ella_mod(word a, word b, word line, word column);

static int fits_i32(word n) { return n >= INT32_MIN && n <= INT32_MAX; }

// `//`, `%` and `**` on i32s: as on i64s (on the sign-extended words), then checked to fit in 32 bits.
word ella_floordiv_i32(word a, word b, word line, word column) {
    word q = ella_floordiv(a, b, line, column);
    if (!fits_i32(q)) ella_overflow32(a, b, "//", line, column);
    return q;
}

word ella_mod_i32(word a, word b, word line, word column) { return ella_mod(a, b, line, column); }

word ella_pow_i32(word a, word b, word line, word column) {
    if (b < 0) fail(line, column, "an i32 can't be raised to a negative power");
    if ((a > 1 || a < -1) && b >= 32) ella_overflow32(a, b, "**", line, column);
    word result = 1;
    for (word i = 0; i < b; i++) {
        if (__builtin_mul_overflow(result, a, &result) || !fits_i32(result)) ella_overflow32(a, b, "**", line, column);
    }
    return result;
}

word ella_pow(word a, word b, word line, word column) {
    if (b < 0) fail(line, column, "an i64 can't be raised to a negative power");
    if (a == 0) return b == 0 ? 1 : 0;
    if (a == 1) return 1;
    if (a == -1) return b % 2 == 0 ? 1 : -1;
    if (b >= 64) ella_overflow(a, b, "**", line, column);
    word result = 1;
    for (word i = 0; i < b; i++) {
        if (__builtin_mul_overflow(result, a, &result)) ella_overflow(a, b, "**", line, column);
    }
    return result;
}

static void check_shift(word b, word line, word column) {
    if (b < 0 || b > 63) fail(line, column, "shift amount must be between 0 and 63, not %" PRId64, b);
}

word ella_shl(word a, word b, word line, word column) {
    check_shift(b, line, column);
    // `a << b` is `a * 2**b`, and like `*` it traps when that doesn't fit.
    __int128 result = (__int128)a * ((__int128)1 << b);
    if (result > INT64_MAX || result < INT64_MIN) ella_overflow(a, b, "<<", line, column);
    return (word)result;
}

word ella_shr(word a, word b, word line, word column) {
    check_shift(b, line, column);
    return a >> b;  // gcc shifts signed numbers arithmetically
}

// Floating point. Values arrive as raw bits, so convert with memcpy.

static double as_f64(word bits) {
    double x;
    memcpy(&x, &bits, sizeof x);
    return x;
}

static word f64_bits(double x) {
    word bits;
    memcpy(&bits, &x, sizeof bits);
    return bits;
}

// An f32 is the low 32 bits of its word (the rest are 0).
static float as_f32(word bits) {
    uint32_t low = (uint32_t)bits;
    float x;
    memcpy(&x, &low, sizeof x);
    return x;
}

static word f32_bits(float x) {
    uint32_t bits;
    memcpy(&bits, &x, sizeof bits);
    return (word)bits;
}

word ella_pow_f64(word a_bits, word b_bits);

// `**` on f32s: as f64s, rounded to an f32 (as ella.operators does it).
word ella_pow_f32(word a_bits, word b_bits) {
    return f32_bits((float)as_f64(ella_pow_f64(f64_bits(as_f32(a_bits)), f64_bits(as_f32(b_bits)))));
}

word ella_pow_f64(word a_bits, word b_bits) {
    double a = as_f64(a_bits), b = as_f64(b_bits);
    if (a == 0.0 && b < 0.0) return f64_bits(INFINITY);  // as ella.operators.float_pow
    return f64_bits(pow(a, b));
}

// Strings

static Str *str_alloc(word len, word depth) {
    Str *s = alloc(sizeof(Str) + (size_t)len * sizeof(uint32_t), depth);
    s->len = len;
    s->chars = (uint32_t *)(s + 1);
    return s;
}

// Characters start..stop of s, shared: a view (made in the arena at `depth`).
static Str *str_view(const Str *s, word start, word stop, word depth) {
    Str *view = alloc(sizeof(Str), depth);
    view->len = stop - start;
    view->chars = s->chars + start;
    return view;
}

Str *ella_str_concat(const Str *a, const Str *b, word depth) {
    Str *s = str_alloc(a->len + b->len, depth);
    memcpy(s->chars, a->chars, (size_t)a->len * sizeof(uint32_t));
    memcpy(s->chars + a->len, b->chars, (size_t)b->len * sizeof(uint32_t));
    return s;
}

// Compare by code point, like Python: negative, zero or positive.
word ella_str_compare(const Str *a, const Str *b) {
    word n = a->len < b->len ? a->len : b->len;
    for (word i = 0; i < n; i++) {
        if (a->chars[i] != b->chars[i]) return a->chars[i] < b->chars[i] ? -1 : 1;
    }
    return a->len < b->len ? -1 : a->len > b->len ? 1 : 0;
}

Str *ella_str_index(const Str *s, word i, word line, word column, word depth) {
    if (i < 0 || i >= s->len)
        fail(line, column, "index %" PRId64 " is out of range for a str of length %" PRId64, i, s->len);
    Str *c = str_alloc(1, depth);
    c->chars[0] = s->chars[i];
    return c;
}

word ella_str_contains(const Str *haystack, const Str *needle) {
    for (word start = 0; start + needle->len <= haystack->len; start++) {
        if (memcmp(haystack->chars + start, needle->chars, (size_t)needle->len * sizeof(uint32_t)) == 0) return 1;
    }
    return 0;
}

// Lists and tuples

List *ella_list_alloc(word len, word depth) {
    List *xs = alloc(sizeof(List) + (size_t)len * sizeof(word), depth);
    xs->len = xs->cap = len;
    xs->items = (word *)(xs + 1);
    return xs;
}

Tuple *ella_tuple_alloc(word len, word depth) {
    Tuple *t = alloc(sizeof(Tuple) + (size_t)len * sizeof(word), depth);
    t->len = t->cap = len;
    return t;
}

List *ella_list_concat(const List *a, const List *b, word depth) {
    List *xs = ella_list_alloc(a->len + b->len, depth);
    memcpy(xs->items, a->items, (size_t)a->len * sizeof(word));
    memcpy(xs->items + a->len, b->items, (size_t)b->len * sizeof(word));
    return xs;
}

static void check_index(const List *xs, word i, word line, word column) {
    if (i < 0 || i >= xs->len)
        fail(line, column, "index %" PRId64 " is out of range for a list of length %" PRId64, i, xs->len);
}

word ella_list_index(const List *xs, word i, word line, word column) {
    check_index(xs, i, line, column);
    return xs->items[i];
}

// `xs[i] = value`, in place. The compiler made sure nothing else shares xs.
void ella_list_set(List *xs, word i, word value, word line, word column) {
    check_index(xs, i, line, column);
    xs->items[i] = value;
}

// Arrays (docs/grammar.md §10, "Arrays"). Arithmetic on them, total() and
// dot() are AVX2 loops that the code generator writes; they call these
// functions to make their result, and to report their errors.

enum { MAX_DIMS = 32 };  // ella.checker.MAX_DIMS
#define MAX_ITEMS ((__int128)1 << 48)  // an array's sizes (those that aren't 0) multiply to less than this

static size_t item_size(word kind) { return kind == F32_ITEMS || kind == I32_ITEMS ? 4 : 8; }

static Array *array_alloc(word ndim, const word *dims, word len, word kind, word depth) {
    size_t header = sizeof(Array) + (size_t)ndim * sizeof(word);
    Array *a = alloc(header + 24 + (size_t)len * item_size(kind), depth);  // objects are 16-byte aligned
    a->len = len;
    a->ndim = ndim;
    a->kind = kind;
    memcpy(a->dims, dims, (size_t)ndim * sizeof(word));
    a->items = (void *)(((uintptr_t)a + header + 31) & ~(uintptr_t)31);
    return a;
}

static Array *array_like(const Array *a, word depth) { return array_alloc(a->ndim, a->dims, a->len, a->kind, depth); }

// Item i, as a word (an f32 in its low 32 bits, an i32 sign-extended, as everywhere).
static word get_item(const Array *a, word i) {
    if (a->kind == F32_ITEMS) return (word)((const uint32_t *)a->items)[i];
    if (a->kind == I32_ITEMS) return (word)((const int32_t *)a->items)[i];
    return ((const word *)a->items)[i];
}

static void set_item(Array *a, word i, word v) {
    if (item_size(a->kind) == 4) {
        ((uint32_t *)a->items)[i] = (uint32_t)v;
    } else {
        ((word *)a->items)[i] = v;
    }
}

static size_t items_bytes(const Array *a) { return (size_t)a->len * item_size(a->kind); }


static int same_shape(const Array *a, const Array *b) {
    return a->ndim == b->ndim && memcmp(a->dims, b->dims, (size_t)a->ndim * sizeof(word)) == 0;
}

// Numbers as error messages show them: a shape "[2, 3]", or indexes "(1, 5)"
// (and one index alone, "5"), between `open` and `close`.
typedef struct {
    char text[22 * MAX_DIMS + 4];
} Sizes;

static Sizes sizes_text(const word *sizes, word n, const char *open, const char *close) {
    Sizes s;
    char *p = s.text + sprintf(s.text, "%s", open);
    for (word i = 0; i < n; i++) p += sprintf(p, i ? ", %" PRId64 : "%" PRId64, sizes[i]);
    sprintf(p, "%s", close);
    return s;
}

static Sizes shape_text(const Array *a) { return sizes_text(a->dims, a->ndim, "[", "]"); }

// The number of items of an array of the given shape, if it can have that shape.
static word check_shape(const List *shape, word line, word column) {
    if (shape->len == 0) fail(line, column, "an array's shape needs at least one size, not []");
    if (shape->len > MAX_DIMS)
        fail(line, column, "an array has at most %d dimensions, not %" PRId64, MAX_DIMS, shape->len);
    for (word i = 0; i < shape->len; i++) {
        if (shape->items[i] < 0)
            fail(line, column, "an array's sizes can't be negative: %s", sizes_text(shape->items, shape->len, "[", "]").text);
    }
    __int128 bound = 1;
    word len = 1;
    for (word i = 0; i < shape->len; i++) {
        word size = shape->items[i];
        bound *= size > 0 ? size : 1;
        if (bound >= MAX_ITEMS)
            fail(line, column, "an array of shape %s is too big", sizes_text(shape->items, shape->len, "[", "]").text);
        len *= size;
    }
    return len;
}

// The indexes of a failed `array_offset`, which the code generator stores here.
word ella_indexes[MAX_DIMS];

// `a[i, j, ...]` with `count` indexes, which are wrong for its shape.
_Noreturn void ella_array_index_error(const Array *a, word count, word line, word column) {
    if (count != a->ndim) {
        fail(line, column, "an array of shape %s needs %" PRId64 " index%s, not %" PRId64, shape_text(a).text, a->ndim,
             a->ndim == 1 ? "" : "es", count);
    }
    Sizes index = count == 1 ? sizes_text(ella_indexes, 1, "", "") : sizes_text(ella_indexes, count, "(", ")");
    fail(line, column, "index %s is out of range for an array of shape %s", index.text, shape_text(a).text);
}

// `p op q` for one item, with an i64 overflow's error.
static word item_op(word op, word p, word q, word kind, word line, word column) {
    word v;
    if (kind == F64_ITEMS) {
        double f = as_f64(p), g = as_f64(q);
        return f64_bits(op == '+' ? f + g : op == '-' ? f - g : op == '*' ? f * g : f / g);
    }
    if (kind == F32_ITEMS) {  // (single precision, rounded once, as ella.operators does it through f64)
        float f = as_f32(p), g = as_f32(q);
        return f32_bits(op == '+' ? f + g : op == '-' ? f - g : op == '*' ? f * g : f / g);
    }
    if (op == '+' ? __builtin_add_overflow(p, q, &v) : op == '-' ? __builtin_sub_overflow(p, q, &v)
                                                     : __builtin_mul_overflow(p, q, &v)) {
        char symbol[2] = {(char)op, 0};
        ella_overflow(p, q, symbol, line, column);  // (i32s can't overflow 64 bits)
    }
    if (kind == I32_ITEMS && !fits_i32(v)) {
        char symbol[2] = {(char)op, 0};
        ella_overflow32(p, q, symbol, line, column);
    }
    return v;
}

// -p for one item.
static word item_neg(word p, word kind, word line, word column) {
    if (kind == F64_ITEMS) return f64_bits(-as_f64(p));
    if (kind == F32_ITEMS) return f32_bits(-as_f32(p));
    if (kind == I64_ITEMS && p == INT64_MIN) ella_overflow_neg(p, line, column);
    if (kind == I32_ITEMS && p == INT32_MIN) ella_overflow32_neg(p, line, column);
    return -p;
}

// The shape arrays of shapes x and y broadcast to (ella.operators.broadcast),
// in `dims`, giving its number of dimensions, or -1 if they don't.
static word broadcast_shape(const Array *x, const Array *y, word *dims) {
    word n = x->ndim > y->ndim ? x->ndim : y->ndim;
    for (word d = 0; d < n; d++) {
        word i = d - (n - x->ndim), j = d - (n - y->ndim);
        word p = i >= 0 ? x->dims[i] : 1, q = j >= 0 ? y->dims[j] : 1;
        if (p != q && p != 1 && q != 1) return -1;
        dims[d] = p == 1 ? q : p;
    }
    return n;
}

// For each dimension of r, how far apart x's items are along it: 0 where x has size 1 (or no such dimension).
static void spread_strides(const Array *x, const Array *r, word *strides) {
    word stride = 1;
    for (word d = r->ndim - 1; d >= 0; d--) {
        word i = d - (r->ndim - x->ndim);
        word size = i >= 0 ? x->dims[i] : 1;
        strides[d] = size == 1 ? 0 : stride;
        stride *= size;
    }
}

// `x op y` on arrays of different shapes that broadcast together, into r, item by item in row-major order.
static void broadcast_items(Array *r, const Array *x, const Array *y, word op, word line, word column) {
    word sx[MAX_DIMS], sy[MAX_DIMS], counters[MAX_DIMS] = {0};
    spread_strides(x, r, sx);
    spread_strides(y, r, sy);
    word i = 0, j = 0;
    for (word k = 0; k < r->len; k++) {
        set_item(r, k, item_op(op, get_item(x, i), get_item(y, j), r->kind, line, column));
        for (word d = r->ndim - 1; d >= 0; d--) {  // the next item: like an odometer
            i += sx[d], j += sy[d];
            if (++counters[d] < r->dims[d]) break;
            i -= sx[d] * r->dims[d], j -= sy[d] * r->dims[d];
            counters[d] = 0;
        }
    }
}

// Whether ella_array_like made the whole result itself (for arrays of different shapes).
word ella_array_done;

// `a[i, 1..3, ...]`: a new array of the items at those indexes, from ella_indexes,
// where bit k of `ranges` says index k is a range (ella.operators.array_slice).
Array *ella_array_slice(const Array *a, word count, word ranges, word line, word column, word depth) {
    if (count > a->ndim) ella_array_index_error(a, count, line, column);
    word starts[MAX_DIMS], sizes[MAX_DIMS], dims[MAX_DIMS], ndim = 0, len = 1;
    int bad = 0;
    for (word k = 0; k < count; k++) {
        __int128 start = ella_indexes[k], stop = start + 1;
        if (ranges >> k & 1) {
            const Range *r = (const Range *)ella_indexes[k];
            start = r->start, stop = r->stop;
        }
        if (!(0 <= start && start <= stop && stop <= a->dims[k])) bad = 1;
        starts[k] = (word)start, sizes[k] = (word)(stop - start);
        if (ranges >> k & 1) dims[ndim++] = sizes[k];
        len *= sizes[k];
    }
    for (word k = count; k < a->ndim; k++) {  // the dimensions left out are whole
        starts[k] = 0, sizes[k] = dims[ndim++] = a->dims[k];
        len *= sizes[k];
    }
    if (bad) {  // "index (0, 1..5)": each index as ella.values.to_text shows it
        char text[64 * MAX_DIMS + 4], *p = text;
        if (count > 1) *p++ = '(';
        for (word k = 0; k < count; k++) {
            if (k) p += sprintf(p, ", ");
            if (ranges >> k & 1) {
                const Range *r = (const Range *)ella_indexes[k];  // its stop is at most 2**63 (`..=` the largest i64)
                if (r->stop > INT64_MAX) {
                    p += sprintf(p, "%" PRId64 "..9223372036854775808", r->start);
                } else {
                    p += sprintf(p, "%" PRId64 "..%" PRId64, r->start, (word)r->stop);
                }
            } else {
                p += sprintf(p, "%" PRId64, ella_indexes[k]);
            }
        }
        if (count > 1) *p++ = ')';
        *p = 0;
        fail(line, column, "index %s is out of range for an array of shape %s", text, shape_text(a).text);
    }
    if (ndim == 0) dims[ndim++] = 1;  // (never: a slice has a range)
    Array *r = array_alloc(ndim, dims, len, a->kind, depth);
    word counters[MAX_DIMS] = {0}, strides[MAX_DIMS], stride = 1, from = 0;
    count = a->ndim;
    for (word k = count - 1; k >= 0; k--) {
        strides[k] = stride;
        stride *= a->dims[k];
        from += starts[k] * strides[k];
    }
    for (word i = 0; i < len; i++) {
        set_item(r, i, get_item(a, from));
        for (word k = count - 1; k >= 0; k--) {  // the next item: like an odometer
            from += strides[k];
            if (++counters[k] < sizes[k]) break;
            from -= strides[k] * sizes[k];
            counters[k] = 0;
        }
    }
    return r;
}

// The new array for arithmetic on `a` and `b`, either of which may be a number
// instead of an array: bit 0 of `flags` says a is an array, bit 1 b; its bits
// from 8 up are the operator. Two arrays of
// different shapes that broadcast together are worked out here, item by item
// (ella_array_done is then 1); ones that don't are an error.
Array *ella_array_like(word a, word b, word flags, word line, word column, word depth) {
    const Array *x = flags & 1 ? (const Array *)a : NULL, *y = flags & 2 ? (const Array *)b : NULL;
    ella_array_done = 0;
    if (x && y && !same_shape(x, y)) {
        word dims[MAX_DIMS], n = broadcast_shape(x, y, dims);
        if (n < 0) {
            fail(line, column, "'%c' needs arrays whose shapes broadcast together, not %s and %s", (int)(flags >> 8),
                 shape_text(x).text, shape_text(y).text);
        }
        word len = 1;
        __int128 bound = 1;
        for (word d = 0; d < n; d++) len *= dims[d], bound *= dims[d] > 0 ? dims[d] : 1;
        if (bound >= MAX_ITEMS) fail(line, column, "an array of shape %s is too big", sizes_text(dims, n, "[", "]").text);
        Array *r = array_alloc(n, dims, len, x->kind, depth);
        broadcast_items(r, x, y, flags >> 8, line, column);
        ella_array_done = 1;
        return r;
    }
    return array_like(x ? x : y, depth);
}

// i64 (or i32) arithmetic on arrays found an overflow: report the first, in
// order, as ella.operators.array_op does. `op` is "+", "-", "*", or "neg" for `-b`.
_Noreturn void ella_array_overflow(word a, word b, word flags, const char *op, word line, word column) {
    const Array *x = flags & 1 ? (const Array *)a : NULL, *y = flags & 2 ? (const Array *)b : NULL;
    word len = x ? x->len : y->len, kind = x ? x->kind : y->kind;
    for (word i = 0; i < len; i++) {
        word p = x ? get_item(x, i) : a, q = y ? get_item(y, i) : b;
        if (op[0] == 'n') {
            item_neg(q, kind, line, column);
        } else {
            item_op(op[0], p, q, kind, line, column);
        }
    }
    fail(line, column, "an overflow went missing");  // never: the vector code found one
}

// total() and dot() add up into four sums, item i into sum i % 4, which are
// then added as (s0 + s2) + (s1 + s3), as AVX2 code does (ella.library.lane_sum).
static word add_checked(word a, word b, word line, word column) {
    word r;
    if (__builtin_add_overflow(a, b, &r)) ella_overflow(a, b, "+", line, column);
    return r;
}

static word lane_sum_i64(const word *sums, word line, word column) {
    word low = add_checked(sums[0], sums[2], line, column), high = add_checked(sums[1], sums[3], line, column);
    return add_checked(low, high, line, column);
}

static word lane_sum_i32(const word *sums, word line, word column) {
    word low = item_op('+', sums[0], sums[2], I32_ITEMS, line, column);
    word high = item_op('+', sums[1], sums[3], I32_ITEMS, line, column);
    return item_op('+', low, high, I32_ITEMS, line, column);
}

// total() of an Array[i64] overflowed: report where, as ella.library.lane_sum.
_Noreturn void ella_total_overflow(const Array *a, word line, word column) {
    word sums[4] = {0, 0, 0, 0};
    for (word i = 0; i < a->len; i++) sums[i % 4] = add_checked(sums[i % 4], get_item(a, i), line, column);
    lane_sum_i64(sums, line, column);
    fail(line, column, "an overflow went missing");
}

static void check_same_shape(const char *name, const Array *a, const Array *b, word line, word column) {
    if (!same_shape(a, b))
        fail(line, column, "%s() needs arrays of the same shape, not %s and %s", name, shape_text(a).text, shape_text(b).text);
}

void ella_check_same_shape(const Array *a, const Array *b, const char *name, word line, word column) {
    check_same_shape(name, a, b, line, column);
}

// Fused arithmetic (ella.passes.fuse): several steps on arrays, which the code
// generator does in one AVX2 loop. It describes them in a plan, and puts their
// operands in ella_fused_operands first. A step's op is '+', '-', '*', '/', 'n'
// for `-left`, and as the last step 't' for `total(left)` or 'd' for
// `dot(left, right)`; an operand n >= 0 is the instruction's operand n, -1 - k
// the result of step k. A step whose array code after it needs too keeps it:
// output `kept` (else -1), made in the arena at ella_fused_depths[kept], and
// given in ella_fused_outs[kept].
typedef struct {
    word op, left, right, line, column, kept;
} FusedStep;

typedef struct {
    word kind;  // of the items: I64_ITEMS, F64_ITEMS or F32_ITEMS
    word operands;
    word arrays;  // bit n: operand n is an array
    word count;
    FusedStep steps[];
} FusedPlan;

word ella_fused_operands[8];
word ella_fused_done;  // whether ella_fused_start did the steps itself, one by one
word ella_fused_depths[4], ella_fused_outs[4];

// total() of the items, in four sums (see lane_sum_i64): floats may be added in any order (docs/grammar.md §9).
static word sum_items(const Array *a, word line, word column) {
    if (a->kind == F64_ITEMS) {
        double sums[4] = {0.0, 0.0, 0.0, 0.0};
        for (word i = 0; i < a->len; i++) sums[i % 4] += as_f64(get_item(a, i));
        return f64_bits((sums[0] + sums[2]) + (sums[1] + sums[3]));
    }
    if (a->kind == F32_ITEMS) {
        float sums[4] = {0.0f, 0.0f, 0.0f, 0.0f};
        for (word i = 0; i < a->len; i++) sums[i % 4] += as_f32(get_item(a, i));
        return f32_bits((sums[0] + sums[2]) + (sums[1] + sums[3]));
    }
    word sums[4] = {0, 0, 0, 0};
    for (word i = 0; i < a->len; i++) sums[i % 4] = item_op('+', sums[i % 4], get_item(a, i), a->kind, line, column);
    return a->kind == I32_ITEMS ? lane_sum_i32(sums, line, column) : lane_sum_i64(sums, line, column);
}

// One step on whole arrays, with its errors, as ella.operators.array_op does it (and ella.library's total and
// dot): an array in the arena at `depth`, or for total and dot a number.
static word fused_step(const FusedStep *s, word a, int a_array, word b, int b_array, word depth) {
    const Array *x = (const Array *)a, *y = (const Array *)b;
    if (s->op == 't') return sum_items(x, s->line, s->column);
    if (s->op == 'd') {
        check_same_shape("dot", x, y, s->line, s->column);
        Array *products = array_like(x, depth);
        for (word i = 0; i < x->len; i++) set_item(products, i, item_op('*', get_item(x, i), get_item(y, i), x->kind, s->line, s->column));
        return sum_items(products, s->line, s->column);
    }
    if (s->op == 'n') {
        Array *r = array_like(x, depth);
        for (word i = 0; i < x->len; i++) set_item(r, i, item_neg(get_item(x, i), x->kind, s->line, s->column));
        return (word)r;
    }
    Array *r = ella_array_like(a, b, a_array | b_array << 1 | s->op << 8, s->line, s->column, depth);
    if (ella_array_done) return (word)r;  // broadcast
    for (word i = 0; i < r->len; i++) {
        word p = a_array ? get_item(x, i) : a, q = b_array ? get_item(y, i) : b;
        set_item(r, i, item_op(s->op, p, q, r->kind, s->line, s->column));
    }
    return (word)r;
}

// The steps one by one, in the arena at `depth`: the last one's result. This
// stops at the first error they make, as the steps before fusion would.
static word fused_steps(const FusedPlan *plan, word depth) {
    word results[16];
    for (word k = 0; k < plan->count; k++) {
        const FusedStep *s = &plan->steps[k];
        word refs[2] = {s->left, s->right}, values[2] = {0, 0};
        int arrays[2] = {0, 0};
        for (int j = 0; j < (s->op == 'n' || s->op == 't' ? 1 : 2); j++) {
            word n = refs[j];
            values[j] = n >= 0 ? ella_fused_operands[n] : results[-1 - n];
            arrays[j] = n < 0 || (plan->arrays >> n & 1);
        }
        results[k] = fused_step(s, values[0], arrays[0], values[1], arrays[1],
                                s->kept >= 0 ? ella_fused_depths[s->kept] : depth);
        if (s->kept >= 0) ella_fused_outs[s->kept] = results[k];
    }
    return results[plan->count - 1];
}

// The fused loop found an i64 overflow: do the steps one by one, which stops at the first.
_Noreturn void ella_fused_fail(const FusedPlan *plan) {
    fused_steps(plan, 0);
    const FusedStep *last = &plan->steps[plan->count - 1];
    fail(last->line, last->column, "an error went missing");  // never: the fused loop found one
}

// The array the fused steps make (none if they end in a sum), if their arrays
// all have the same shape. Otherwise the steps are done one by one here, in
// the arena at `depth`, and ella_fused_done says so: this gives their result.
word ella_fused_start(const FusedPlan *plan, word depth) {
    const Array *first = NULL;
    ella_fused_done = 0;
    for (word n = 0; n < plan->operands; n++) {
        if (!(plan->arrays >> n & 1)) continue;
        const Array *a = (const Array *)ella_fused_operands[n];
        if (first == NULL) {
            first = a;
        } else if (!same_shape(first, a)) {
            ella_fused_done = 1;
            return fused_steps(plan, depth);
        }
    }
    for (word k = 0; k < plan->count; k++) {  // the arrays steps keep
        word kept = plan->steps[k].kept;
        if (kept >= 0) ella_fused_outs[kept] = (word)array_like(first, ella_fused_depths[kept]);
    }
    word last = plan->steps[plan->count - 1].op;
    return last == 't' || last == 'd' ? 0 : (word)array_like(first, depth);
}

// The library's array functions. Most work on the words of the items, whatever
// their type, and have one name for each item type, as the code generator calls them.

static Array *array_of(const List *xs, word kind, word depth) {
    Array *a = array_alloc(1, &xs->len, xs->len, kind, depth);
    for (word i = 0; i < xs->len; i++) set_item(a, i, xs->items[i]);
    return a;
}

static Array *fill(const List *shape, word x, word kind, word line, word column, word depth) {
    word len = check_shape(shape, line, column);
    Array *a = array_alloc(shape->len, shape->items, len, kind, depth);
    for (word i = 0; i < len; i++) set_item(a, i, x);
    return a;
}

static Array *reshape(const Array *a, const List *shape, word line, word column, word depth) {
    if (check_shape(shape, line, column) != a->len) {
        fail(line, column, "reshape() can't make an array of shape %s from one of shape %s",
             sizes_text(shape->items, shape->len, "[", "]").text, shape_text(a).text);
    }
    Array *result = array_alloc(shape->len, shape->items, a->len, a->kind, depth);
    memcpy(result->items, a->items, items_bytes(a));
    return result;
}

static List *shape(const Array *a, word depth) {
    List *xs = ella_list_alloc(a->ndim, depth);
    memcpy(xs->items, a->dims, (size_t)a->ndim * sizeof(word));
    return xs;
}

static List *flat(const Array *a, word depth) {
    List *xs = ella_list_alloc(a->len, depth);
    for (word i = 0; i < a->len; i++) xs->items[i] = get_item(a, i);
    return xs;
}

static Array *transpose(const Array *a, word line, word column, word depth) {
    if (a->ndim != 2) fail(line, column, "transpose() needs a 2-dimensional array, not one of shape %s", shape_text(a).text);
    word rows = a->dims[0], columns = a->dims[1], dims[2] = {columns, rows};
    Array *result = array_alloc(2, dims, a->len, a->kind, depth);
    for (word i = 0; i < rows; i++) {
        for (word j = 0; j < columns; j++) set_item(result, j * rows + i, get_item(a, i * columns + j));
    }
    return result;
}

// sums(a, axis): the items added up along one dimension, which the result doesn't have, each sum from 0 in
// order (ella.library.sums), so that every backend gives the same bits.
static Array *sums(const Array *a, word axis, word line, word column, word depth) {
    if (a->ndim < 2)
        fail(line, column, "sums() needs an array of 2 or more dimensions, not one of shape %s", shape_text(a).text);
    if (axis < 0 || axis >= a->ndim)
        fail(line, column, "sums() can't add along axis %" PRId64 " of an array of shape %s", axis, shape_text(a).text);
    word dims[MAX_DIMS], outer = 1, inner = 1, n = a->dims[axis];
    for (word i = 0; i < a->ndim; i++) {
        if (i < axis) outer *= a->dims[i];
        if (i > axis) inner *= a->dims[i];
        if (i != axis) dims[i < axis ? i : i - 1] = a->dims[i];
    }
    Array *result = array_alloc(a->ndim - 1, dims, outer * inner, a->kind, depth);
    word zero = a->kind == F64_ITEMS ? f64_bits(0.0) : a->kind == F32_ITEMS ? f32_bits(0.0f) : 0;
    for (word o = 0; o < outer; o++) {
        for (word i = 0; i < inner; i++) {
            word total = zero;
            for (word k = 0; k < n; k++) total = item_op('+', total, get_item(a, (o * n + k) * inner + i), a->kind, line, column);
            set_item(result, o * inner + i, total);
        }
    }
    return result;
}

static char *encode(const Str *s);

// The run-time check of a shape (ella.library.match_shape): `expected` has a size, -1 for any, or -2 - i for the
// same size as dimension i, for each dimension. Gives a's shape.
static List *match_shape(const Array *a, const List *expected, const Str *what, word line, word column, word depth) {
    word wanted[MAX_DIMS + 1];
    int ok = expected->len == a->ndim;
    word shown = expected->len < MAX_DIMS ? expected->len : MAX_DIMS;
    for (word i = 0; i < shown; i++) {
        word size = expected->items[i];
        wanted[i] = size <= -2 && -2 - size < a->ndim ? a->dims[-2 - size] : size < -1 ? -1 : size;
        if (wanted[i] >= 0 && i < a->ndim && wanted[i] != a->dims[i]) ok = 0;
    }
    if (!ok) {
        char text[24 * MAX_DIMS + 4], *p = text + sprintf(text, "[");
        for (word i = 0; i < shown; i++) {
            if (i) p += sprintf(p, ", ");
            p += wanted[i] < 0 ? sprintf(p, "_") : sprintf(p, "%" PRId64, wanted[i]);
        }
        sprintf(p, "]");
        fail(line, column, "%s should have shape %s, not %s", encode(what), text, shape_text(a).text);
    }
    return shape(a, depth);
}

// matmul(a, b): the result, of shape [n, m], for a of shape [n, k] and b of [k, m].
static Array *matmul_result(const Array *a, const Array *b, word line, word column, word depth) {
    if (a->ndim != 2 || b->ndim != 2) {
        fail(line, column, "matmul() needs two 2-dimensional arrays, not shapes %s and %s", shape_text(a).text,
             shape_text(b).text);
    }
    if (a->dims[1] != b->dims[0])
        fail(line, column, "matmul() can't multiply arrays of shapes %s and %s", shape_text(a).text, shape_text(b).text);
    word dims[2] = {a->dims[0], b->dims[1]};
    return array_alloc(2, dims, a->dims[0] * b->dims[1], a->kind, depth);
}

typedef double __attribute__((may_alias)) f64_word;  // an item of an Array[f64], read as the number it is

// Item (i, j) is the sum of a[i, p] * b[p, j], from p = 0 up (ella.library.matmul
// does them in that order; native code may round differently: docs/grammar.md §9).
// A block of 4 rows by 8 columns of the result stays in eight AVX2 registers
// while p goes up, each row of b's 8 items loaded once for the 4 rows, and
// each product added with one rounding (a fused multiply-add). The rows and
// columns left over go one item at a time.
__attribute__((target("avx2,fma"))) static void matmul_rows(const f64_word *a, const f64_word *b, f64_word *c,
                                                             word n, word k, word m) {
    word i = 0;
    for (; i + 4 <= n; i += 4) {
        word j = 0;
        for (; j + 8 <= m; j += 8) {
            __m256d sums[4][2];
            for (int r = 0; r < 4; r++) sums[r][0] = sums[r][1] = _mm256_setzero_pd();
            for (word p = 0; p < k; p++) {
                __m256d low = _mm256_loadu_pd((const double *)(b + p * m + j));
                __m256d high = _mm256_loadu_pd((const double *)(b + p * m + j + 4));
                for (int r = 0; r < 4; r++) {
                    __m256d x = _mm256_set1_pd(a[(i + r) * k + p]);
                    sums[r][0] = _mm256_fmadd_pd(x, low, sums[r][0]);
                    sums[r][1] = _mm256_fmadd_pd(x, high, sums[r][1]);
                }
            }
            for (int r = 0; r < 4; r++) {
                _mm256_storeu_pd((double *)(c + (i + r) * m + j), sums[r][0]);
                _mm256_storeu_pd((double *)(c + (i + r) * m + j + 4), sums[r][1]);
            }
        }
        for (; j < m; j++) {
            for (word r = i; r < i + 4; r++) {
                double sum = 0.0;
                for (word p = 0; p < k; p++) sum = __builtin_fma(a[r * k + p], b[p * m + j], sum);
                c[r * m + j] = sum;
            }
        }
    }
    for (; i < n; i++) {
        for (word j = 0; j < m; j++) {
            double sum = 0.0;
            for (word p = 0; p < k; p++) sum = __builtin_fma(a[i * k + p], b[p * m + j], sum);
            c[i * m + j] = sum;
        }
    }
}

Array *ella_lib_matmul_f64(const Array *a, const Array *b, word line, word column, word depth) {
    Array *c = matmul_result(a, b, line, column, depth);
    matmul_rows((const f64_word *)a->items, (const f64_word *)b->items, (f64_word *)c->items, a->dims[0], a->dims[1],
                b->dims[1]);
    return c;
}

// Integers have no AVX2 multiply that notices overflow, and must check each
// operation, in the order ella.library.matmul does them.
static Array *matmul_ints(const Array *a, const Array *b, word line, word column, word depth) {
    Array *c = matmul_result(a, b, line, column, depth);
    word n = a->dims[0], k = a->dims[1], m = b->dims[1];
    for (word i = 0; i < n; i++) {
        for (word j = 0; j < m; j++) {
            word total = 0;
            for (word p = 0; p < k; p++) {
                word product = item_op('*', get_item(a, i * k + p), get_item(b, p * m + j), a->kind, line, column);
                total = item_op('+', total, product, a->kind, line, column);
            }
            set_item(c, i * m + j, total);
        }
    }
    return c;
}

Array *ella_lib_matmul_i64(const Array *a, const Array *b, word line, word column, word depth) {
    return matmul_ints(a, b, line, column, depth);
}

Array *ella_lib_matmul_i32(const Array *a, const Array *b, word line, word column, word depth) {
    return matmul_ints(a, b, line, column, depth);
}

// dot() of two arrays of integers, in four sums like total().
static word dot_ints(const Array *a, const Array *b, word line, word column) {
    check_same_shape("dot", a, b, line, column);
    word sums[4] = {0, 0, 0, 0};
    for (word i = 0; i < a->len; i++) {
        word product = item_op('*', get_item(a, i), get_item(b, i), a->kind, line, column);
        sums[i % 4] = item_op('+', sums[i % 4], product, a->kind, line, column);
    }
    return a->kind == I32_ITEMS ? lane_sum_i32(sums, line, column) : lane_sum_i64(sums, line, column);
}

word ella_lib_dot_i64(const Array *a, const Array *b, word line, word column, word depth) {
    UNUSED(depth);
    return dot_ints(a, b, line, column);
}

word ella_lib_dot_i32(const Array *a, const Array *b, word line, word column, word depth) {
    UNUSED(depth);
    return dot_ints(a, b, line, column);
}

word ella_lib_total_i32(const Array *a, word line, word column, word depth) {
    UNUSED(depth);
    return sum_items(a, line, column);
}

// matmul of Array[f32]s, as matmul_rows: blocks of 4 rows by 8 columns, each a
// vector of 8 f32s, the products added with fused multiply-adds.
typedef float __attribute__((may_alias)) f32_item;

__attribute__((target("avx2,fma"))) static void matmul_rows_f32(const f32_item *a, const f32_item *b, f32_item *c,
                                                                 word n, word k, word m) {
    word i = 0;
    for (; i + 4 <= n; i += 4) {
        word j = 0;
        for (; j + 8 <= m; j += 8) {
            __m256 sums[4];
            for (int r = 0; r < 4; r++) sums[r] = _mm256_setzero_ps();
            for (word p = 0; p < k; p++) {
                __m256 row = _mm256_loadu_ps((const float *)(b + p * m + j));
                for (int r = 0; r < 4; r++) sums[r] = _mm256_fmadd_ps(_mm256_set1_ps(a[(i + r) * k + p]), row, sums[r]);
            }
            for (int r = 0; r < 4; r++) _mm256_storeu_ps((float *)(c + (i + r) * m + j), sums[r]);
        }
        for (; j < m; j++) {
            for (word r = i; r < i + 4; r++) {
                float sum = 0.0f;
                for (word p = 0; p < k; p++) sum = __builtin_fmaf(a[r * k + p], b[p * m + j], sum);
                c[r * m + j] = sum;
            }
        }
    }
    for (; i < n; i++) {
        for (word j = 0; j < m; j++) {
            float sum = 0.0f;
            for (word p = 0; p < k; p++) sum = __builtin_fmaf(a[i * k + p], b[p * m + j], sum);
            c[i * m + j] = sum;
        }
    }
}

Array *ella_lib_matmul_f32(const Array *a, const Array *b, word line, word column, word depth) {
    Array *c = matmul_result(a, b, line, column, depth);
    matmul_rows_f32((const f32_item *)a->items, (const f32_item *)b->items, (f32_item *)c->items, a->dims[0],
                    a->dims[1], b->dims[1]);
    return c;
}

// For the AVX2 code: masks for loading the last n % 4 items of 8 bytes (or n % 8 of 4 bytes), for each n % 4
// (or n % 8): the other lanes are 0.
__attribute__((aligned(32))) const uint64_t ella_tail_masks[4][4] = {
    {0, 0, 0, 0}, {~0ull, 0, 0, 0}, {~0ull, ~0ull, 0, 0}, {~0ull, ~0ull, ~0ull, 0}};
__attribute__((aligned(32))) const uint32_t ella_tail_masks32[8][8] = {
    {0}, {~0u}, {~0u, ~0u}, {~0u, ~0u, ~0u}, {~0u, ~0u, ~0u, ~0u}, {~0u, ~0u, ~0u, ~0u, ~0u},
    {~0u, ~0u, ~0u, ~0u, ~0u, ~0u}, {~0u, ~0u, ~0u, ~0u, ~0u, ~0u, ~0u}};

#define ARRAY_FUNCTIONS(T, KIND)                                                                              \
    Array *ella_lib_array_##T(const List *xs, word line, word column, word depth) {                           \
        UNUSED(line), UNUSED(column);                                                                         \
        return array_of(xs, KIND, depth);                                                                     \
    }                                                                                                         \
    Array *ella_lib_fill_##T(const List *s, word x, word line, word column, word depth) {                     \
        return fill(s, x, KIND, line, column, depth);                                                         \
    }                                                                                                         \
    Array *ella_lib_reshape_##T(const Array *a, const List *s, word line, word column, word depth) {          \
        return reshape(a, s, line, column, depth);                                                            \
    }                                                                                                         \
    List *ella_lib_shape_##T(const Array *a, word line, word column, word depth) {                            \
        UNUSED(line), UNUSED(column);                                                                         \
        return shape(a, depth);                                                                               \
    }                                                                                                         \
    List *ella_lib_flat_##T(const Array *a, word line, word column, word depth) {                             \
        UNUSED(line), UNUSED(column);                                                                         \
        return flat(a, depth);                                                                                \
    }                                                                                                         \
    Array *ella_lib_transpose_##T(const Array *a, word line, word column, word depth) {                       \
        return transpose(a, line, column, depth);                                                             \
    }                                                                                                         \
    Array *ella_lib_sums_##T(const Array *a, word axis, word line, word column, word depth) {                 \
        return sums(a, axis, line, column, depth);                                                            \
    }                                                                                                         \
    List *ella_lib_match_shape_##T(const Array *a, const List *e, const Str *w, word l, word c, word depth) { \
        return match_shape(a, e, w, l, c, depth);                                                             \
    }

ARRAY_FUNCTIONS(i64, I64_ITEMS)
ARRAY_FUNCTIONS(f64, F64_ITEMS)
ARRAY_FUNCTIONS(f32, F32_ITEMS)
ARRAY_FUNCTIONS(i32, I32_ITEMS)

// Ranges

Range *ella_range(word start, word stop, word depth) {
    Range *r = alloc(sizeof(Range), depth);
    r->start = start;
    r->stop = stop;
    return r;
}

Range *ella_range_incl(word start, word last, word depth) {
    Range *r = ella_range(start, last, depth);
    r->stop = (__int128)last + 1;
    return r;
}

word ella_range_len(const Range *r) { return r->stop > r->start ? (word)(r->stop - r->start) : 0; }

word ella_range_index(const Range *r, word i) { return r->start + i; }

word ella_range_contains(const Range *r, word x) { return x >= r->start && x < r->stop; }

// Enums and closures

Variant *ella_variant_alloc(word tag, word count, word depth) {
    Variant *v = alloc(sizeof(Variant) + (size_t)count * sizeof(word), depth);
    v->tag = tag;
    v->count = count;
    return v;
}

Closure *ella_closure_alloc(void *code, word count, const Type *types, word depth) {
    Closure *c = alloc(sizeof(Closure) + (size_t)count * sizeof(word), depth);
    c->code = code;
    c->count = count;
    c->types = types;
    return c;
}

// Copying: `copy v @r` makes a deep copy of v in the arena at `depth`, so that
// it shares nothing with v. Strings are copied too, since v's may be in an
// arena that is freed sooner.

static int is_boxed(const Type *t) {
    return t->kind != K_UNIT && t->kind != K_I64 && t->kind != K_I32 && t->kind != K_F64 && t->kind != K_F32 &&
           t->kind != K_BOOL && t->kind != K_HANDLE && t->kind != K_REF;
}

static word copy_inner(word v, const Type *t, word depth) {
    switch (t->kind) {
    case K_STR: {
        const Str *s = (const Str *)v;
        Str *copy = str_alloc(s->len, depth);
        memcpy(copy->chars, s->chars, (size_t)s->len * sizeof(uint32_t));
        return (word)copy;
    }
    case K_RANGE: {
        const Range *r = (const Range *)v;
        Range *copy = alloc(sizeof(Range), depth);
        *copy = *r;
        return (word)copy;
    }
    case K_LIST: {
        const List *xs = (const List *)v;
        List *copy = ella_list_alloc(xs->len, depth);
        const Type *item = t->parts[0];
        for (word i = 0; i < xs->len; i++) {
            copy->items[i] = is_boxed(item) ? copy_inner(xs->items[i], item, depth) : xs->items[i];
        }
        return (word)copy;
    }
    case K_TUPLE: {
        const Tuple *xs = (const Tuple *)v;
        Tuple *copy = ella_tuple_alloc(xs->len, depth);
        for (word i = 0; i < xs->len; i++) {
            const Type *item = t->parts[i];
            copy->items[i] = is_boxed(item) ? copy_inner(xs->items[i], item, depth) : xs->items[i];
        }
        return (word)copy;
    }
    case K_ARRAY: {
        const Array *a = (const Array *)v;
        Array *copy = array_like(a, depth);
        memcpy(copy->items, a->items, items_bytes(a));
        return (word)copy;
    }
    case K_ENUM: {
        const Variant *x = (const Variant *)v;
        const VariantType *variant = t->parts[x->tag];
        Variant *copy = ella_variant_alloc(x->tag, x->count, depth);
        for (word i = 0; i < x->count; i++) {
            const Type *field = variant->fields[i];
            copy->fields[i] = is_boxed(field) ? copy_inner(x->fields[i], field, depth) : x->fields[i];
        }
        return (word)copy;
    }
    case K_FUNCTION: {
        const Closure *c = (const Closure *)v;
        Closure *copy = ella_closure_alloc(c->code, c->count, c->types, depth);
        for (word i = 0; i < c->count; i++) {
            const Type *item = c->types->parts[i];
            copy->captured[i] = is_boxed(item) ? copy_inner(c->captured[i], item, depth) : c->captured[i];
        }
        return (word)copy;
    }
    default:  // (), i64, f64, bool, handles, refs; pools and arenas can't be copied
        return v;
    }
}

word ella_copy(word v, const Type *t, word depth) {
    size_t before = in_use;
    word copy = copy_inner(v, t, depth);
    copied_bytes += in_use - before;
    return copy;
}

// `move v @r`: a value that can't be copied, moved to another arena: copied
// there, with the finalizers of the values it holds moving with them. The
// old value is never used again.
word ella_move(word v, const Type *t, word depth) {
    switch (t->kind) {
    case K_TUPLE: {
        const Tuple *xs = (const Tuple *)v;
        Tuple *moved = ella_tuple_alloc(xs->len, depth);
        for (word i = 0; i < xs->len; i++) moved->items[i] = ella_move(xs->items[i], t->parts[i], depth);
        return (word)moved;
    }
    case K_ENUM: {
        const Variant *x = (const Variant *)v;
        const VariantType *variant = t->parts[x->tag];
        Variant *moved = ella_variant_alloc(x->tag, x->count, depth);
        for (word i = 0; i < x->count; i++) moved->fields[i] = ella_move(x->fields[i], variant->fields[i], depth);
        if (variant->finalize != NULL) {
            unregister(v);
            ella_register((word)moved, variant->finalize, depth);
        }
        return (word)moved;
    }
    case K_POOL: {
        // A pool's header moves; its values stay in their slot arenas.
        word *moved = alloc(6 * sizeof(word), depth);
        memcpy(moved, (const void *)v, 6 * sizeof(word));
        unregister(v);
        ella_register((word)moved, NULL, depth);
        return (word)moved;
    }
    case K_ARENA: {
        // An arena's header moves; its values stay in its slot arena.
        word *moved = alloc(4 * sizeof(word), depth);
        memcpy(moved, (const void *)v, 4 * sizeof(word));
        unregister(v);
        ella_register((word)moved, SUB_ARENA, depth);
        return (word)moved;
    }
    case K_GEN: {
        // A generator moves with its frame; the values its levels hold stay in its slot arenas.
        size_t size = gen_size((const Gen *)v);
        Gen *moved = alloc(size, depth);
        memcpy(moved, (const void *)v, size);
        unregister(v);
        ella_register((word)moved, GEN, depth);
        return (word)moved;
    }
    default:
        return is_boxed(t) ? ella_copy(v, t, depth) : v;
    }
}

// `carry v @current`: a value a loop keeps for its next iteration, copied
// into the iteration's arena if it is still in the carried one, which is
// about to be freed. (Then all of it is: the loop's values never hold parts
// of the carried arena otherwise.)
word ella_carry(word v, const Type *t, word carried, word current) {
    return ella_region_of(v) == carried ? ella_copy(v, t, current) : v;
}

// Storing `value` into an object that may be a pool's value (`@slot`): a
// pool's value lives in its slot's arena, so the value is copied there.
word ella_store_slot(word object, word value, const Type *t) {
    word depth = ella_region_of(object);
    if (depth < SLOT_DEPTH || !is_boxed(t)) return value;
    return ella_copy(value, t, depth);
}

// Pools. Each slot holds a value in its own slot arena or is free. Handles
// encode a process-unique pool id, slot and generation, so using a handle
// with another pool is caught too. Pool ids are never reused.

typedef struct {
    word generation;
    word depth;  // its slot arena, while it holds a value
    word value;
    word next_free;  // while it is free: the next free slot, or -1
} Slot;

typedef struct {
    word live;  // how many slots hold values
    word len, capacity;
    Slot *slots;
    word free;  // the most recently freed slot, or -1
    word id;
} Pool;

word ella_pool_new(word depth) {
    Pool *pool = alloc(sizeof(Pool), depth);
    if (pool_ids_used == POOL_IDS) out_of_memory("too many pools");
    *pool = (Pool){0, 0, 0, NULL, -1, pool_ids_used++};
    ella_register((word)pool, NULL, depth);  // when it dies, its values are freed
    return (word)pool;
}

static word handle(word pool, word slot, word generation) {
    return (word)(((uint64_t)pool << 40) | ((uint64_t)generation << 20) | (uint64_t)slot);
}

word ella_alloc(Pool *pool, word value, const Type *t) {
    word slot;
    if (pool->free >= 0) {
        slot = pool->free;
        pool->free = pool->slots[slot].next_free;
    } else {
        if (pool->len == HANDLE_SLOTS) out_of_memory("a pool holds too many values");
        if (pool->len == pool->capacity) {
            pool->capacity = pool->capacity ? 2 * pool->capacity : 8;
            pool->slots = realloc(pool->slots, (size_t)pool->capacity * sizeof(Slot));
            if (pool->slots == NULL) out_of_memory("can't grow a pool");
        }
        slot = pool->len++;
        pool->slots[slot].generation = 0;
    }
    Slot *s = &pool->slots[slot];
    s->depth = new_slot_arena();
    // A value that can't be copied is moved, with its finalizers.
    s->value = ella_move(value, t, s->depth);
    pool->live++;
    return handle(pool->id, slot, s->generation);
}

static Slot *slot_of(Pool *pool, word h, word line, word column) {
    word slot = (word)((uint64_t)h & 0xFFFFF);
    word generation = (word)(((uint64_t)h >> 20) & 0xFFFFF);
    word pool_id = (word)((uint64_t)h >> 40);
    if (pool_id != pool->id) fail(line, column, "handle belongs to another pool");
    if (slot >= pool->len || pool->slots[slot].generation != generation || pool->slots[slot].depth < 0) {
        fail(line, column, "handle <handle %" PRId64 ":%" PRId64 "> is stale: its value was freed", slot, generation);
    }
    return &pool->slots[slot];
}

word ella_pool_get(Pool *pool, word h, word line, word column) { return slot_of(pool, h, line, column)->value; }

void ella_pool_set(Pool *pool, word h, word value, const Type *t, word line, word column) {
    Slot *s = slot_of(pool, h, line, column);
    // Copy the new value into a new arena before freeing the old one: it may share parts of the old value.
    word depth = new_slot_arena();
    s->value = is_boxed(t) ? ella_copy(value, t, depth) : value;
    free_slot_arena(s->depth);
    s->depth = depth;
}

void ella_free(Pool *pool, word h, word line, word column) {
    Slot *s = slot_of(pool, h, line, column);
    word slot = s - pool->slots;
    word depth = s->depth;
    s->depth = -1;
    if (s->generation < HANDLE_GENERATIONS - 1) {
        s->generation++;
        s->next_free = pool->free;
        pool->free = slot;
    }
    pool->live--;
    free_slot_arena(depth);
}

word ella_pool_len(const Pool *pool) { return pool->live; }

// A pool dies: free its values, last slot first.
static void free_pool(word object) {
    Pool *pool = (Pool *)object;
    for (word slot = pool->len - 1; slot >= 0; slot--) {
        if (pool->slots[slot].depth >= 0) {
            word depth = pool->slots[slot].depth;
            pool->slots[slot].depth = -1;
            free_slot_arena(depth);
        }
    }
    free(pool->slots);
    pool->slots = NULL;
    pool->len = pool->live = 0;
}

// Sub-arenas: an Ella `Arena`. All its values live in one slot arena, and
// are freed together, which bumps the slot arena's generation. A ref is one
// word: the slot arena's number (less SLOT_DEPTH) in the high 20 bits, its
// generation (24 bits) in the next 24, and which of the arena's
// values in the low 20. So a ref is checked against the arena with one
// comparison, which also catches a ref to another arena.

#define REF_INDEXES ((word)1 << 20)

typedef struct {
    word depth;  // its slot arena
    word len, capacity;
    word *values;
} SubArena;

word ella_arena_new(word depth) {
    SubArena *arena = alloc(sizeof(SubArena), depth);
    *arena = (SubArena){new_slot_arena(), 0, 0, NULL};
    ella_register((word)arena, SUB_ARENA, depth);  // when it dies, its slot arena is freed
    return (word)arena;
}

static word ref_index(word ref) { return (word)((uint64_t)ref % REF_INDEXES); }

static word ref_base(const SubArena *arena) {
    word generation = slot_arenas[arena->depth - SLOT_DEPTH].generation;
    return (word)(((uint64_t)(arena->depth - SLOT_DEPTH) << 44) | ((uint64_t)generation << 20));
}

word ella_arena_alloc(SubArena *arena, word value, const Type *t) {
    if (arena->len == REF_INDEXES) out_of_memory("an arena holds too many values");
    if (arena->len == arena->capacity) {
        arena->capacity = arena->capacity ? 2 * arena->capacity : 8;
        arena->values = realloc(arena->values, (size_t)arena->capacity * sizeof(word));
        if (arena->values == NULL) out_of_memory("can't grow an arena");
    }
    // A value that can't be copied is moved, with its finalizers.
    arena->values[arena->len] = ella_move(value, t, arena->depth);
    return ref_base(arena) | arena->len++;
}

static word *value_of(SubArena *arena, word ref, word line, word column) {
    word index = ref_index(ref);
    if (ref - index != ref_base(arena) || index >= arena->len) {
        fail(line, column, "ref <ref %" PRId64 "> is stale: its arena was freed, or it is another arena's", index);
    }
    return &arena->values[index];
}

word ella_arena_get(SubArena *arena, word ref, word line, word column) { return *value_of(arena, ref, line, column); }

// The new value is copied into the arena; the old one stays there until the arena is freed.
void ella_arena_set(SubArena *arena, word ref, word value, const Type *t, word line, word column) {
    word *place = value_of(arena, ref, line, column);
    *place = is_boxed(t) ? ella_copy(value, t, arena->depth) : value;
}

// `free(mut arena)`: free all its values at once, keeping the slot arena for its next ones.
void ella_arena_free(SubArena *arena) {
    arena->len = 0;
    free_arena(arena->depth);
    // A ref's generation has 24 bits. Give this live Arena a fresh slot
    // arena instead of allowing its old refs to become valid after wrap.
    if (arena->depth >= SLOT_DEPTH && slot_arenas[arena->depth - SLOT_DEPTH].generation >= REF_GENERATIONS)
        arena->depth = new_slot_arena();
}

word ella_arena_len(const SubArena *arena) { return arena->len; }

// An arena dies: free its values, and give back its slot arena.
static void free_sub_arena(word object) {
    SubArena *arena = (SubArena *)object;
    free_slot_arena(arena->depth);
    free(arena->values);
    arena->values = NULL;
    arena->len = 0;
}

// Generators

// `gen_new`: a generator for the body at `code`, starting at `start`, with a
// slot arena for each of its `levels` and a frame of `size` bytes, made in
// the arena at `depth`. The generated code then fills in its arguments.
Gen *ella_gen_new(void *code, void *start, word levels, word size, word depth) {
    Gen *g = alloc(sizeof(Gen) + (size_t)levels * sizeof(word) + (size_t)size, depth);
    g->code = code;
    g->resume = start;
    g->levels = levels;
    g->size = size;
    for (word i = 0; i < levels; i++) g->depths[i] = new_slot_arena();
    memset(&g->depths[levels], 0, (size_t)size);
    ella_register((word)g, GEN, depth);  // when it dies, its levels are freed
    return g;
}

// An argument of a new generator: a copy of it in its level 0, or the value
// itself, moved there, if it can't be copied.
word ella_gen_arg(const Gen *g, word value, const Type *t) { return ella_move(value, t, g->depths[0]); }

// `reset k` in a generator's body: free its levels k and up, deepest first,
// keeping their slot arenas.
void ella_gen_reset(const Gen *g, word first) {
    for (word i = g->levels - 1; i >= first; i--) free_arena(g->depths[i]);
}

// `swap k` in a generator's body, as ella_swap: `first` is k + 2.
void ella_gen_swap(const Gen *g, word first, word current, word carried) {
    ella_gen_reset(g, first);
    finalize_all(arena(current));
    free_arena(carried);
}

// A generator dies: its levels' values are finalized, deepest level first,
// and its slot arenas given back.
static void free_gen(word object) {
    Gen *g = (Gen *)object;
    for (word i = g->levels - 1; i >= 0; i--) free_slot_arena(g->depths[i]);
    g->levels = 0;
}

// `xs += ys`: add copies of ys's items to xs, which lives in the arena at
// `depth`, and return xs, which may have moved. If xs is the last object in
// its arena (and not a view), it grows in place; otherwise it moves to a new
// home with room for twice as many items. Either way, adding an item costs
// O(1) on average.
List *ella_list_append(List *xs, const List *ys, const Type *item, word depth) {
    if (depth < 0) depth = ella_region_of((word)xs);  // @slot: the arena xs is in
    word n = ys->len, len = xs->len;  // ys may be xs, so read its length first
    if (len + n > xs->cap) {
        word cap = len + n > 2 * xs->cap ? len + n : 2 * xs->cap;
        if (cap < 4) cap = 4;
        Arena *a = arena(depth);
        char *end = (char *)xs + round16(sizeof(List) + (size_t)xs->cap * sizeof(word));
        if (end == a->top && xs->items == (word *)(xs + 1)) {
            char *new_end = (char *)xs + round16(sizeof(List) + (size_t)cap * sizeof(word));
            if ((size_t)(new_end - arena_start(depth)) > arena_limit(depth)) out_of_memory("an arena is full");
            in_use += (size_t)(new_end - end);
            if (in_use > peak) peak = in_use;
            a->top = new_end;
            xs->cap = cap;
        } else {
            List *moved = alloc(sizeof(List) + (size_t)cap * sizeof(word), depth);
            moved->items = (word *)(moved + 1);
            memcpy(moved->items, xs->items, (size_t)len * sizeof(word));
            moved->len = len;
            moved->cap = cap;
            xs = moved;
        }
    }
    for (word i = 0; i < n; i++) {
        xs->items[len + i] = is_boxed(item) ? ella_copy(ys->items[i], item, depth) : ys->items[i];
    }
    xs->len = len + n;
    return xs;
}

// Equality, like ella.values.equal: lists, tuples and enum values are equal
// when their parts are.

word ella_equal(word a, word b, const Type *t) {
    switch (t->kind) {
    case K_UNIT:
        return 1;
    case K_F64:
        return as_f64(a) == as_f64(b);
    case K_F32:
        return as_f32(a) == as_f32(b);
    case K_STR:
        return ella_str_compare((const Str *)a, (const Str *)b) == 0;
    case K_RANGE: {
        // Like Python's ranges: equal if they hold the same numbers.
        const Range *x = (const Range *)a, *y = (const Range *)b;
        word x_len = ella_range_len(x), y_len = ella_range_len(y);
        return x_len == y_len && (x_len == 0 || x->start == y->start);
    }
    case K_LIST: {
        const List *x = (const List *)a, *y = (const List *)b;
        if (x->len != y->len) return 0;
        for (word i = 0; i < x->len; i++) {
            if (!ella_equal(x->items[i], y->items[i], t->parts[0])) return 0;
        }
        return 1;
    }
    case K_TUPLE: {
        const Tuple *x = (const Tuple *)a, *y = (const Tuple *)b;
        for (word i = 0; i < t->count; i++) {
            if (!ella_equal(x->items[i], y->items[i], t->parts[i])) return 0;
        }
        return 1;
    }
    case K_ARRAY: {
        const Array *x = (const Array *)a, *y = (const Array *)b;
        if (!same_shape(x, y)) return 0;
        for (word i = 0; i < x->len; i++) {
            if (!ella_equal(get_item(x, i), get_item(y, i), t->parts[0])) return 0;
        }
        return 1;
    }
    case K_ENUM: {
        const Variant *x = (const Variant *)a, *y = (const Variant *)b;
        const VariantType *first = t->parts[0];
        if (first->eq) return ella_call2(first->eq, a, b, 0, &ella_eq_site) & 1;
        if (x->tag != y->tag) return 0;
        const VariantType *variant = t->parts[x->tag];
        for (word i = 0; i < (variant->error ? 2 : variant->count); i++) {
            if (!ella_equal(x->fields[i], y->fields[i], variant->fields[i])) return 0;
        }
        return 1;
    }
    default:  // i64, bool, and functions (by identity)
        return a == b;
    }
}

word ella_list_contains(const List *xs, word item, const Type *list_type) {
    for (word i = 0; i < xs->len; i++) {
        if (ella_equal(xs->items[i], item, list_type->parts[0])) return 1;
    }
    return 0;
}

// Text form, like ella.values.to_text. Text is built up in a growing buffer
// of code points.

typedef struct {
    word len, capacity;
    uint32_t *chars;
} Buffer;

// Buffers are scratch space, not Ella values, so they use malloc and free.
static void put(Buffer *b, uint32_t c) {
    if (b->len == b->capacity) {
        b->capacity = b->capacity ? b->capacity * 2 : 32;
        b->chars = realloc(b->chars, (size_t)b->capacity * sizeof(uint32_t));
        if (b->chars == NULL) out_of_memory("can't grow a text buffer");
    }
    b->chars[b->len++] = c;
}

static void put_ascii(Buffer *b, const char *text) {
    while (*text) put(b, (unsigned char)*text++);
}

static void put_i128(Buffer *b, __int128 n) {
    char digits[48];
    int len = 0;
    int negative = n < 0;
    unsigned __int128 u = negative ? -(unsigned __int128)n : (unsigned __int128)n;
    do {
        digits[len++] = (char)('0' + (int)(u % 10));
        u /= 10;
    } while (u);
    if (negative) put(b, '-');
    while (len) put(b, (uint32_t)digits[--len]);
}

// A float the way Python's repr() writes it: the fewest digits that read
// back as the same number, in plain notation for moderate exponents and
// scientific notation otherwise: 0.1, 2.5, 1e-05, 1e+16, 1.0.
static void put_f64(Buffer *b, double x) {
    if (isnan(x)) return put_ascii(b, "nan");
    if (isinf(x)) return put_ascii(b, x > 0 ? "inf" : "-inf");

    // The shortest scientific form that round-trips: try more and more
    // digits until reading the text back gives exactly x. 17 always does.
    char text[40];
    for (int precision = 0; precision <= 16; precision++) {
        snprintf(text, sizeof text, "%.*e", precision, x);
        if (strtod(text, NULL) == x) break;
    }
    // text is now like "-1.2345e+02": take the digits and the exponent apart.
    const char *p = text;
    if (*p == '-') {
        put(b, '-');
        p++;
    }
    char digits[24];
    int n = 0;
    for (; *p != 'e'; p++) {
        if (*p != '.') digits[n++] = *p;
    }
    int exponent = atoi(p + 1);
    while (n > 1 && digits[n - 1] == '0') n--;  // no trailing zeros

    // Python's rule: with the value written as 0.DIGITS x 10**point, use
    // plain notation when -4 < point <= 16.
    int point = exponent + 1;
    if (-4 < point && point <= 16) {
        if (point <= 0) {
            put_ascii(b, "0.");
            for (int i = 0; i < -point; i++) put(b, '0');
            for (int i = 0; i < n; i++) put(b, (uint32_t)digits[i]);
        } else if (point >= n) {
            for (int i = 0; i < n; i++) put(b, (uint32_t)digits[i]);
            for (int i = n; i < point; i++) put(b, '0');
            put_ascii(b, ".0");
        } else {
            for (int i = 0; i < n; i++) {
                if (i == point) put(b, '.');
                put(b, (uint32_t)digits[i]);
            }
        }
    } else {
        put(b, (uint32_t)digits[0]);
        if (n > 1) {
            put(b, '.');
            for (int i = 1; i < n; i++) put(b, (uint32_t)digits[i]);
        }
        char exponent_text[8];
        snprintf(exponent_text, sizeof exponent_text, "e%+03d", exponent);
        put_ascii(b, exponent_text);
    }
}

// An f32's text, as ella.values.f32_text: the shortest decimal that is nearest
// to it of all f32s (going through an f64, as Python does), written as an f64 is.
static void put_f32(Buffer *b, float x) {
    if (!isfinite(x)) {
        put_f64(b, x);
        return;
    }
    char text[32];
    for (int digits = 1; digits <= 9; digits++) {
        snprintf(text, sizeof text, "%.*g", digits, (double)x);
        float back = (float)strtod(text, NULL);
        if (memcmp(&back, &x, sizeof x) == 0) break;
    }
    put_f64(b, strtod(text, NULL));
}

// A string inside another value is quoted the way Python's json.dumps does it.
static void put_quoted(Buffer *b, const Str *s) {
    put(b, '"');
    for (word i = 0; i < s->len; i++) {
        uint32_t c = s->chars[i];
        switch (c) {
        case '"': put_ascii(b, "\\\""); break;
        case '\\': put_ascii(b, "\\\\"); break;
        case '\n': put_ascii(b, "\\n"); break;
        case '\r': put_ascii(b, "\\r"); break;
        case '\t': put_ascii(b, "\\t"); break;
        case '\b': put_ascii(b, "\\b"); break;
        case '\f': put_ascii(b, "\\f"); break;
        default:
            if (c < 0x20) {
                char escape[8];
                snprintf(escape, sizeof escape, "\\u%04x", c);
                put_ascii(b, escape);
            } else {
                put(b, c);
            }
        }
    }
    put(b, '"');
}

static void put_value(Buffer *b, word v, const Type *t, int nested, int custom);

// An array's items as nested lists, one level for each of its `ndim` dimensions:
// `[[1.0, 2.0], [3.0, 4.0]]`, as ella.values.nested_text.
static void put_nested(Buffer *b, const Array *a, word start, const word *dims, word ndim, word len, const Type *item) {
    put(b, '[');
    word step = dims[0] ? len / dims[0] : 0;
    for (word i = 0; i < dims[0]; i++) {
        if (i) put_ascii(b, ", ");
        if (ndim == 1) {
            put_value(b, get_item(a, start + i), item, 1, 0);
        } else {
            put_nested(b, a, start + i * step, dims + 1, ndim - 1, step, item);
        }
    }
    put(b, ']');
}

// `custom`: the text form `print` writes, through `text` methods; without it,
// as in error messages, every value has its built-in form.
static void put_value(Buffer *b, word v, const Type *t, int nested, int custom) {
    switch (t->kind) {
    case K_UNIT: put_ascii(b, "()"); break;
    case K_I64:
    case K_I32: put_i128(b, v); break;  // (an i32 is its word, sign-extended)
    case K_F64: put_f64(b, as_f64(v)); break;
    case K_F32: put_f32(b, as_f32(v)); break;
    case K_BOOL: put_ascii(b, v ? "true" : "false"); break;
    case K_FUNCTION: put_ascii(b, "<function>"); break;
    case K_CODE: put_ascii(b, "<code>"); break;  // never made: native code can't build code at run time
    case K_POOL:
        put_ascii(b, "<pool of ");
        put_i128(b, ((const Pool *)v)->live);
        put(b, '>');
        break;
    case K_GEN: put_ascii(b, "<gen>"); break;
    case K_ARENA:
        put_ascii(b, "<arena of ");
        put_i128(b, ((const SubArena *)v)->len);
        put(b, '>');
        break;
    case K_REF:
        put_ascii(b, "<ref ");
        put_i128(b, ref_index(v));
        put(b, '>');
        break;
    case K_HANDLE:
        put_ascii(b, "<handle ");
        put_i128(b, (word)((uint64_t)v & 0xFFFFF));
        put(b, ':');
        put_i128(b, (word)(((uint64_t)v >> 20) & 0xFFFFF));
        put(b, '>');
        break;
    case K_STR: {
        const Str *s = (const Str *)v;
        if (nested) {
            put_quoted(b, s);
        } else {
            for (word i = 0; i < s->len; i++) put(b, s->chars[i]);
        }
        break;
    }
    case K_RANGE: {
        const Range *r = (const Range *)v;
        put_i128(b, r->start);
        put_ascii(b, "..");
        put_i128(b, r->stop);
        break;
    }
    case K_LIST: {
        const List *xs = (const List *)v;
        put(b, '[');
        for (word i = 0; i < xs->len; i++) {
            if (i) put_ascii(b, ", ");
            put_value(b, xs->items[i], t->parts[0], 1, custom);
        }
        put(b, ']');
        break;
    }
    case K_ARRAY: {
        const Array *a = (const Array *)v;
        put_ascii(b, "array(");
        put_nested(b, a, 0, a->dims, a->ndim, a->len, t->parts[0]);
        put(b, ')');
        break;
    }
    case K_TUPLE: {
        const Tuple *xs = (const Tuple *)v;
        put(b, '(');
        for (word i = 0; i < t->count; i++) {
            if (i) put_ascii(b, ", ");
            put_value(b, xs->items[i], t->parts[i], 1, custom);
        }
        if (t->count == 1) put(b, ',');
        put(b, ')');
        break;
    }
    case K_ENUM: {
        const Variant *x = (const Variant *)v;
        const VariantType *variant = t->parts[x->tag];
        if (custom && variant->text) {
            // Its text method's result goes in a level of its own, freed once it has been read.
            word scratch = ella_enter(1);
            const Str *s = (const Str *)ella_call1(variant->text, v, scratch, &ella_text_site);
            for (word i = 0; i < s->len; i++) put(b, s->chars[i]);
            ella_leave(scratch);
            break;
        }
        put_ascii(b, variant->name);
        if (variant->error) {  // Error("message"), or Error("message", cause)
            put(b, '(');
            put_value(b, x->fields[0], variant->fields[0], 1, custom);
            const Variant *cause = (const Variant *)x->fields[1];
            if (cause->count) {
                put_ascii(b, ", ");
                put_value(b, cause->fields[0], t, 1, custom);
            }
            put(b, ')');
        } else if (variant->count) {
            put(b, '(');
            for (word i = 0; i < variant->count; i++) {
                if (i) put_ascii(b, ", ");
                put_value(b, x->fields[i], variant->fields[i], 1, custom);
            }
            put(b, ')');
        }
        break;
    }
    }
}

// A buffer's text as a new string. The buffer is freed.
static Str *buffer_str(Buffer *b, word depth) {
    Str *s = str_alloc(b->len, depth);
    if (b->len) memcpy(s->chars, b->chars, (size_t)b->len * sizeof(uint32_t));
    free(b->chars);
    *b = (Buffer){0};
    return s;
}

Str *ella_to_text(word v, const Type *t, word depth) {
    Buffer b = {0};
    put_value(&b, v, t, 0, 1);
    return buffer_str(&b, depth);
}

// Output, in UTF-8

static void write_utf8(FILE *f, const uint32_t *chars, word len) {
    for (word i = 0; i < len; i++) {
        uint32_t c = chars[i];
        if (c < 0x80) {
            fputc((int)c, f);
        } else if (c < 0x800) {
            fputc((int)(0xC0 | (c >> 6)), f);
            fputc((int)(0x80 | (c & 0x3F)), f);
        } else if (c < 0x10000) {
            fputc((int)(0xE0 | (c >> 12)), f);
            fputc((int)(0x80 | ((c >> 6) & 0x3F)), f);
            fputc((int)(0x80 | (c & 0x3F)), f);
        } else {
            fputc((int)(0xF0 | (c >> 18)), f);
            fputc((int)(0x80 | ((c >> 12) & 0x3F)), f);
            fputc((int)(0x80 | ((c >> 6) & 0x3F)), f);
            fputc((int)(0x80 | (c & 0x3F)), f);
        }
    }
}

void ella_print(word v, const Type *t) {
    Buffer b = {0};
    put_value(&b, v, t, 0, 1);
    write_utf8(stdout, b.chars, b.len);
    fputc('\n', stdout);
    free(b.chars);
}

// `eprint`: the same on standard error, after what was printed so far.
void ella_eprint(word v, const Type *t) {
    fflush(stdout);
    Buffer b = {0};
    put_value(&b, v, t, 0, 1);
    write_utf8(stderr, b.chars, b.len);
    fputc('\n', stderr);
    free(b.chars);
}

// Stop with a run-time error whose message is in a buffer.
_Noreturn static void fail_with(word line, word column, Buffer *message) {
    word depth = start_error(line, column);
    write_utf8(stderr, message->chars, message->len);
    end_error(depth);
}

// A trap whose message shows a value: "no case matches {}".
_Noreturn void ella_trap_value(const char *message, word v, const Type *t, word line, word column) {
    Buffer b = {0};
    put_value(&b, v, t, 1, 0);
    const char *hole = strstr(message, "{}");
    word depth = start_error(line, column);
    fprintf(stderr, "%.*s", (int)(hole - message), message);
    write_utf8(stderr, b.chars, b.len);
    fputs(hole + 2, stderr);
    end_error(depth);
}

// Slices: `xs[a..b]` and `s[a..b]`, views of the list's items or the
// string's characters (docs/memory-model.md §18). The compiler copies them
// where sharing could be seen.

static void check_slice(const Range *r, word len, const char *what, word line, word column) {
    if (0 <= r->start && r->start <= r->stop && r->stop <= len) return;
    Buffer b = {0};
    put_i128(&b, r->start);
    put_ascii(&b, "..");
    put_i128(&b, r->stop);
    char text[100];
    for (word i = 0; i < b.len; i++) text[i] = (char)b.chars[i];
    text[b.len] = 0;
    free(b.chars);
    fail(line, column, "slice %s is out of range for a %s of length %" PRId64, text, what, len);
}

static Str *substring(const Str *s, word start, word stop, word depth) {
    Str *part = str_alloc(stop - start, depth);
    memcpy(part->chars, s->chars + start, (size_t)(stop - start) * sizeof(uint32_t));
    return part;
}

Str *ella_str_slice(const Str *s, const Range *r, word line, word column, word depth) {
    check_slice(r, s->len, "str", line, column);
    return str_view(s, r->start, (word)r->stop, depth);
}

List *ella_list_slice(const List *xs, const Range *r, word line, word column, word depth) {
    check_slice(r, xs->len, "list", line, column);
    List *view = alloc(sizeof(List), depth);
    view->len = (word)r->stop - r->start;
    view->cap = 0;
    view->items = xs->items + r->start;
    return view;
}

// The library: built-in functions with fixed types, which must behave
// exactly like ella/library.py. Each takes its arguments, then the line and
// column of the call (for errors), then the depth of the arena for its
// result.


// The variants of the built-in enums, numbered by their positions in
// ella.checker.BUILTIN_ENUMS.
enum { TAG_OK = 0, TAG_ERR = 1, TAG_SOME = 0, TAG_NONE = 1, TAG_ERROR = 0 };

static word variant1(word tag, word field, word depth) {
    Variant *v = ella_variant_alloc(tag, 1, depth);
    v->fields[0] = field;
    return (word)v;
}

static word some(word value, word depth) { return variant1(TAG_SOME, value, depth); }
static word none(word depth) { return (word)ella_variant_alloc(TAG_NONE, 0, depth); }
static word ok(word value, word depth) { return variant1(TAG_OK, value, depth); }

static Str *place_str(word line, word column, word depth);

// `main` returned Err(error), a built-in Error: stop with it, where it was made,
// then each of its causes on a line of its own (as ella.values.main_error).
_Noreturn void ella_main_error(const Variant *error) {
    fflush(stdout);
    for (const char *before = "";; before = "  caused by ") {
        const Str *message = (const Str *)error->fields[0], *at = (const Str *)error->fields[2];
        fputs(before, stderr);
        write_utf8(stderr, at->chars, at->len);
        fputs(": ", stderr);
        write_utf8(stderr, message->chars, message->len);
        fputc('\n', stderr);
        const Variant *cause = (const Variant *)error->fields[1];
        if (cause->tag != TAG_SOME) break;
        error = (const Variant *)cause->fields[0];
    }
    exit(1);
}

// Err(Error(message)), with the message in a buffer, which is freed, made at
// the call at line and column (where an error there would be reported).
static word err(Buffer *message, word line, word column, word depth) {
    Variant *error = ella_variant_alloc(TAG_ERROR, 3, depth);
    error->fields[0] = (word)buffer_str(message, depth);
    error->fields[1] = none(depth);
    error->fields[2] = (word)place_str(line, column, depth);
    return variant1(TAG_ERR, (word)error, depth);
}

// One UTF-8 sequence at the start of p (n bytes): its length, and its code
// point in *c. 0 if it isn't valid: as Python's decoder, no overlong forms,
// surrogates or code points past U+10FFFF.
static size_t utf8_sequence(const unsigned char *p, size_t n, uint32_t *c) {
    size_t len;
    uint32_t least;
    if (p[0] < 0x80) {
        *c = p[0];
        return 1;
    } else if (p[0] >= 0xC2 && p[0] <= 0xDF) {
        len = 2, least = 0x80, *c = p[0] & 0x1F;
    } else if (p[0] >= 0xE0 && p[0] <= 0xEF) {
        len = 3, least = 0x800, *c = p[0] & 0x0F;
    } else if (p[0] >= 0xF0 && p[0] <= 0xF4) {
        len = 4, least = 0x10000, *c = p[0] & 0x07;
    } else {
        return 0;
    }
    if (n < len) return 0;
    for (size_t i = 1; i < len; i++) {
        if ((p[i] & 0xC0) != 0x80) return 0;
        *c = (*c << 6) | (p[i] & 0x3F);
    }
    if (*c < least || *c > 0x10FFFF || (*c >= 0xD800 && *c <= 0xDFFF)) return 0;
    return len;
}

// UTF-8 text as a new string, or NULL if it isn't valid UTF-8.
static Str *decode(const unsigned char *bytes, size_t n, word depth) {
    word count = 0;
    uint32_t c;
    for (size_t i = 0; i < n; count++) {
        size_t len = utf8_sequence(bytes + i, n - i, &c);
        if (len == 0) return NULL;
        i += len;
    }
    Str *s = str_alloc(count, depth);
    for (size_t i = 0, k = 0; i < n; k++) {
        i += utf8_sequence(bytes + i, n - i, &c);
        s->chars[k] = c;
    }
    return s;
}

// Where a call is, as text for an Error's `at` (as print_place writes it).
// 0:0 is in a function made for a library function used as a value: the place
// is its call's, as for an error there (start_error).
static Str *place_str(word line, word column, word depth) {
    if (line == 0 && column == 0 && site_below(ella_depth) >= 1) {
        const Site *site = ella_trace[site_below(ella_depth)];
        line = site->line, column = site->column;
    }
    word file = line >> 32;
    char text[4096];
    int n = snprintf(text, sizeof text, "%s%s%" PRId64 ":%" PRId64, file ? ella_files[file] : "", file ? ":" : "",
                     line & 0xffffffff, column);
    Str *s = decode((const unsigned char *)text, (size_t)n < sizeof text ? (size_t)n : sizeof text - 1, depth);
    if (s == NULL) out_of_memory("a file name isn't valid UTF-8");
    return s;
}

// A string in UTF-8, with a 0 after it, for file names and file contents.
// Its length in bytes goes to *len, if len isn't NULL. The caller frees it.
static char *encode_len(const Str *s, size_t *len) {
    char *text = malloc((size_t)s->len * 4 + 1), *p = text;
    if (text == NULL) out_of_memory("can't encode a string");
    for (word i = 0; i < s->len; i++) {
        uint32_t c = s->chars[i];
        if (c < 0x80) {
            *p++ = (char)c;
        } else if (c < 0x800) {
            *p++ = (char)(0xC0 | (c >> 6));
            *p++ = (char)(0x80 | (c & 0x3F));
        } else if (c < 0x10000) {
            *p++ = (char)(0xE0 | (c >> 12));
            *p++ = (char)(0x80 | ((c >> 6) & 0x3F));
            *p++ = (char)(0x80 | (c & 0x3F));
        } else {
            *p++ = (char)(0xF0 | (c >> 18));
            *p++ = (char)(0x80 | ((c >> 12) & 0x3F));
            *p++ = (char)(0x80 | ((c >> 6) & 0x3F));
            *p++ = (char)(0x80 | (c & 0x3F));
        }
    }
    *p = 0;
    if (len != NULL) *len = (size_t)(p - text);
    return text;
}

static char *encode(const Str *s) { return encode_len(s, NULL); }

static int is_digit(uint32_t c) { return c >= '0' && c <= '9'; }

static int matches_at(const Str *s, word i, const Str *part) {
    return i + part->len <= s->len && memcmp(s->chars + i, part->chars, (size_t)part->len * sizeof(uint32_t)) == 0;
}

word ella_lib_to_f64_i64(word n, word line, word column, word depth) {
    UNUSED(line), UNUSED(column), UNUSED(depth);
    return f64_bits((double)n);
}

word ella_lib_to_f64_f64(word x, word line, word column, word depth) {
    UNUSED(line), UNUSED(column), UNUSED(depth);
    return x;
}

word ella_lib_to_f64_f32(word x, word line, word column, word depth) {
    UNUSED(line), UNUSED(column), UNUSED(depth);
    return f64_bits((double)as_f32(x));
}

// to_f32: the nearest f32 (an i64 by way of the nearest f64, as Python does it).
word ella_lib_to_f32_i64(word n, word line, word column, word depth) {
    UNUSED(line), UNUSED(column), UNUSED(depth);
    return f32_bits((float)(double)n);
}

word ella_lib_to_f32_f64(word x, word line, word column, word depth) {
    UNUSED(line), UNUSED(column), UNUSED(depth);
    return f32_bits((float)as_f64(x));
}

word ella_lib_to_f32_f32(word x, word line, word column, word depth) {
    UNUSED(line), UNUSED(column), UNUSED(depth);
    return x;
}

// A float's text, as print writes it (an f32 as an f32), for error messages.
static void float_text(char *text, double x, int f32) {
    Buffer b = {0};
    if (f32) {
        put_f32(&b, (float)x);
    } else {
        put_f64(&b, x);
    }
    for (word i = 0; i < b.len; i++) text[i] = (char)b.chars[i];
    text[b.len] = 0;
    free(b.chars);
}

// A float without its fraction, if that fits in `bits` bits (to_i64 and to_i32).
static word truncated(double x, int f32, int bits, word line, word column) {
    double whole = trunc(x), limit = bits == 64 ? 9223372036854775808.0 : 2147483648.0;
    if (!(whole >= -limit && whole < limit)) {  // also false for NaN
        char text[40];
        float_text(text, x, f32);
        fail(line, column, "to_i%d() can't convert %s to an i%d", bits, text, bits);
    }
    return (word)whole;
}

word ella_lib_to_i64_f64(word bits, word line, word column, word depth) {
    UNUSED(depth);
    return truncated(as_f64(bits), 0, 64, line, column);
}

word ella_lib_to_i64_f32(word bits, word line, word column, word depth) {
    UNUSED(depth);
    return truncated(as_f32(bits), 1, 64, line, column);
}

word ella_lib_to_i64_i64(word n, word line, word column, word depth) {
    UNUSED(line), UNUSED(column), UNUSED(depth);
    return n;
}

word ella_lib_to_i64_i32(word n, word line, word column, word depth) {
    UNUSED(line), UNUSED(column), UNUSED(depth);
    return n;
}

word ella_lib_to_i32_f64(word bits, word line, word column, word depth) {
    UNUSED(depth);
    return truncated(as_f64(bits), 0, 32, line, column);
}

word ella_lib_to_i32_f32(word bits, word line, word column, word depth) {
    UNUSED(depth);
    return truncated(as_f32(bits), 1, 32, line, column);
}

word ella_lib_to_i32_i64(word n, word line, word column, word depth) {
    UNUSED(depth);
    if (!fits_i32(n)) fail(line, column, "to_i32() can't convert %" PRId64 " to an i32", n);
    return n;
}

word ella_lib_to_i32_i32(word n, word line, word column, word depth) {
    UNUSED(line), UNUSED(column), UNUSED(depth);
    return n;
}

word ella_lib_to_f64_i32(word n, word line, word column, word depth) {
    UNUSED(line), UNUSED(column), UNUSED(depth);
    return f64_bits((double)n);
}

word ella_lib_to_f32_i32(word n, word line, word column, word depth) {
    UNUSED(line), UNUSED(column), UNUSED(depth);
    return f32_bits((float)n);
}

word ella_lib_parse_i64(const Str *s, word line, word column, word depth) {
    UNUSED(line), UNUSED(column);
    word i = 0;
    int negative = 0;
    if (i < s->len && (s->chars[i] == '+' || s->chars[i] == '-')) negative = s->chars[i++] == '-';
    int valid = i < s->len, fits = 1;
    __int128 n = 0;
    for (; i < s->len && valid; i++) {
        valid = is_digit(s->chars[i]);
        if (fits) n = n * 10 + (s->chars[i] - '0');
        if (n > (__int128)INT64_MAX + 1) fits = 0;
    }
    if (negative) n = -n;
    if (valid && fits && n <= INT64_MAX) return ok((word)n, depth);
    Buffer message = {0};
    put_quoted(&message, s);
    put_ascii(&message, valid ? " doesn't fit in an i64" : " isn't an i64");
    return err(&message, line, column, depth);
}

word ella_lib_parse_f64(const Str *s, word line, word column, word depth) {
    UNUSED(line), UNUSED(column);
    // [+-]?, then digits with an optional fraction and exponent, or inf or nan.
    word i = 0, n = s->len;
    const uint32_t *c = s->chars;
    if (i < n && (c[i] == '+' || c[i] == '-')) i++;
    int valid;
    if (n - i == 3 && ((c[i] == 'i' && c[i + 1] == 'n' && c[i + 2] == 'f') ||
                       (c[i] == 'n' && c[i + 1] == 'a' && c[i + 2] == 'n'))) {
        valid = 1;
    } else {
        word digits = 0;
        for (; i < n && is_digit(c[i]); i++) digits++;
        if (i < n && c[i] == '.') {
            for (i++; i < n && is_digit(c[i]); i++) digits++;
        }
        valid = digits > 0;
        if (valid && i < n && (c[i] == 'e' || c[i] == 'E')) {
            i++;
            if (i < n && (c[i] == '+' || c[i] == '-')) i++;
            valid = i < n && is_digit(c[i]);
            while (i < n && is_digit(c[i])) i++;
        }
        valid = valid && i == n;
    }
    if (!valid) {
        Buffer message = {0};
        put_quoted(&message, s);
        put_ascii(&message, " isn't an f64");
        return err(&message, line, column, depth);
    }
    char *text = encode(s);  // only ASCII, so one byte per character
    double x = strtod(text, NULL);
    free(text);
    return ok(f64_bits(x), depth);
}

word ella_lib_read_line(word line, word column, word depth) {
    char *text = NULL;
    size_t capacity = 0;
    ssize_t n = getline(&text, &capacity, stdin);
    if (n < 0) {
        free(text);
        return none(depth);
    }
    if (n > 0 && text[n - 1] == '\n') n--;
    if (n > 0 && text[n - 1] == '\r') n--;
    Str *s = decode((const unsigned char *)text, (size_t)n, depth);
    free(text);
    if (s == NULL) fail(line, column, "read_line(): the input isn't valid UTF-8");
    return some((word)s, depth);
}

word ella_lib_read_file(const Str *path, word line, word column, word depth) {
    UNUSED(line), UNUSED(column);
    Buffer message = {0};
    put_ascii(&message, "can't read ");
    put_quoted(&message, path);
    put_ascii(&message, ": ");
    int error = 0;
    for (word i = 0; i < path->len; i++) {
        if (path->chars[i] == 0) error = EINVAL;  // as open() would say, given the whole name
    }
    unsigned char *data = NULL;
    size_t len = 0, capacity = 0;
    if (!error) {
        char *name = encode(path);
        int fd = open(name, O_RDONLY | O_CLOEXEC);
        free(name);
        if (fd < 0) error = errno;
        while (!error) {
            if (len == capacity) {
                capacity = capacity ? 2 * capacity : 4096;
                data = realloc(data, capacity);
                if (data == NULL) out_of_memory("can't read a file");
            }
            ssize_t got = read(fd, data + len, capacity - len);
            if (got < 0) error = errno;
            if (got <= 0) break;
            len += (size_t)got;
        }
        if (fd >= 0) close(fd);
    }
    if (error) {
        put_ascii(&message, strerror(error));
        free(data);
        return err(&message, line, column, depth);
    }
    Str *s = decode(data, len, depth);
    free(data);
    if (s == NULL) {
        put_ascii(&message, "it isn't valid UTF-8");
        return err(&message, line, column, depth);
    }
    free(message.chars);
    return ok((word)s, depth);
}

static int arg_count;
static char **arg_values;

word ella_lib_args(word line, word column, word depth) {
    List *args = ella_list_alloc(arg_count, depth);
    for (int i = 0; i < arg_count; i++) {
        Str *s = decode((const unsigned char *)arg_values[i], strlen(arg_values[i]), depth);
        if (s == NULL) fail(line, column, "args(): an argument isn't valid UTF-8");
        args->items[i] = (word)s;
    }
    return (word)args;
}

// split and trim give views of s's characters (the compiler knows).
word ella_lib_split(const Str *s, const Str *separator, word line, word column, word depth) {
    if (separator->len == 0) fail(line, column, "split() needs a separator that isn't empty");
    word count = 1;
    for (word i = 0; i < s->len;) {
        if (matches_at(s, i, separator)) {
            count++;
            i += separator->len;
        } else {
            i++;
        }
    }
    List *parts = ella_list_alloc(count, depth);
    word start = 0, k = 0;
    for (word i = 0; i < s->len;) {
        if (matches_at(s, i, separator)) {
            parts->items[k++] = (word)str_view(s, start, i, depth);
            i += separator->len;
            start = i;
        } else {
            i++;
        }
    }
    parts->items[k] = (word)str_view(s, start, s->len, depth);
    return (word)parts;
}

Str *ella_lib_join(const List *parts, const Str *separator, word line, word column, word depth) {
    UNUSED(line), UNUSED(column);
    word len = 0;
    for (word i = 0; i < parts->len; i++) len += ((const Str *)parts->items[i])->len + (i ? separator->len : 0);
    Str *s = str_alloc(len, depth);
    uint32_t *p = s->chars;
    for (word i = 0; i < parts->len; i++) {
        const Str *part = (const Str *)parts->items[i];
        if (i) {
            memcpy(p, separator->chars, (size_t)separator->len * sizeof(uint32_t));
            p += separator->len;
        }
        memcpy(p, part->chars, (size_t)part->len * sizeof(uint32_t));
        p += part->len;
    }
    return s;
}

word ella_lib_find(const Str *s, const Str *part, word line, word column, word depth) {
    UNUSED(line), UNUSED(column);
    for (word i = 0; i + part->len <= s->len; i++) {
        if (matches_at(s, i, part)) return some(i, depth);
    }
    return none(depth);
}

Str *ella_lib_replace(const Str *s, const Str *old, const Str *new, word line, word column, word depth) {
    if (old->len == 0) fail(line, column, "replace() needs a string to replace that isn't empty");
    word count = 0;
    for (word i = 0; i < s->len;) {
        if (matches_at(s, i, old)) {
            count++;
            i += old->len;
        } else {
            i++;
        }
    }
    Str *result = str_alloc(s->len + count * (new->len - old->len), depth);
    uint32_t *p = result->chars;
    for (word i = 0; i < s->len;) {
        if (matches_at(s, i, old)) {
            memcpy(p, new->chars, (size_t)new->len * sizeof(uint32_t));
            p += new->len;
            i += old->len;
        } else {
            *p++ = s->chars[i++];
        }
    }
    return result;
}

static int is_space(uint32_t c) { return c == ' ' || c == '\t' || c == '\n' || c == '\r' || c == '\v' || c == '\f'; }

Str *ella_lib_trim(const Str *s, word line, word column, word depth) {
    UNUSED(line), UNUSED(column);
    word start = 0, stop = s->len;
    while (start < stop && is_space(s->chars[start])) start++;
    while (stop > start && is_space(s->chars[stop - 1])) stop--;
    return str_view(s, start, stop, depth);
}

List *ella_lib_chars(const Str *s, word line, word column, word depth) {
    UNUSED(line), UNUSED(column);
    List *chars = ella_list_alloc(s->len, depth);
    for (word i = 0; i < s->len; i++) chars->items[i] = (word)substring(s, i, i + 1, depth);
    return chars;
}

// hash(x), like ella.library.hash_value: 64-bit FNV-1a over a string's code
// points, and over the hashes of the parts of other values, so that values
// ella_equal says are equal hash the same.
static const uint64_t FNV_OFFSET = 0xCBF29CE484222325u, FNV_PRIME = 0x100000001B3u;

static uint64_t fnv(uint64_t h, uint64_t word) { return (h ^ word) * FNV_PRIME; }

static uint64_t hash_value(word v, const Type *t) {
    uint64_t h = FNV_OFFSET;
    switch (t->kind) {
    case K_UNIT:
        return h;
    case K_F64:
    case K_F32: {
        // 0.0 and -0.0 are equal, so they hash the same; NaNs are never equal, but hash alike. (An f32 as the f64 it is.)
        double x = (t->kind == K_F32 ? (double)as_f32(v) : as_f64(v)) + 0.0;
        return fnv(h, isnan(x) ? 0x7FF8000000000000u : (uint64_t)f64_bits(x));
    }
    case K_STR: {
        const Str *s = (const Str *)v;
        for (word i = 0; i < s->len; i++) h = fnv(h, s->chars[i]);
        return h;
    }
    case K_RANGE: {
        // Ranges are equal when they hold the same numbers.
        const Range *r = (const Range *)v;
        word size = ella_range_len(r);
        h = fnv(h, (uint64_t)size);
        return size ? fnv(h, (uint64_t)r->start) : h;
    }
    case K_LIST: {
        const List *xs = (const List *)v;
        for (word i = 0; i < xs->len; i++) h = fnv(h, hash_value(xs->items[i], t->parts[0]));
        return h;
    }
    case K_TUPLE: {
        const Tuple *x = (const Tuple *)v;
        for (word i = 0; i < t->count; i++) h = fnv(h, hash_value(x->items[i], t->parts[i]));
        return h;
    }
    case K_ARRAY: {
        // As the tuple of its shape and its items, (shape(a), flat(a)).
        const Array *a = (const Array *)v;
        uint64_t shape = FNV_OFFSET, items = FNV_OFFSET;
        for (word i = 0; i < a->ndim; i++) shape = fnv(shape, fnv(FNV_OFFSET, (uint64_t)a->dims[i]));
        for (word i = 0; i < a->len; i++) items = fnv(items, hash_value(get_item(a, i), t->parts[0]));
        return fnv(fnv(h, shape), items);
    }
    case K_ENUM: {
        // Its variant's name, without type arguments; an Error without where it was made.
        const Variant *x = (const Variant *)v;
        const VariantType *variant = t->parts[x->tag];
        if (variant->hash) return (uint64_t)ella_call1(variant->hash, v, 0, &ella_hash_site);
        for (const char *c = variant->name; *c && *c != '['; c++) h = fnv(h, (unsigned char)*c);
        for (word i = 0; i < (variant->error ? 2 : variant->count); i++) {
            h = fnv(h, hash_value(x->fields[i], variant->fields[i]));
        }
        return h;
    }
    case K_REF:
        return fnv(h, (uint64_t)ref_index(v));
    case K_HANDLE:
        return fnv(h, ((uint64_t)v & 0xFFFFF) | (((uint64_t)v >> 20 & 0xFFFFF) << 20));
    default:  // i64 and bool
        return fnv(h, (uint64_t)v);
    }
}

word ella_lib_hash(word v, const Type *t, word line, word column, word depth) {
    UNUSED(line), UNUSED(column), UNUSED(depth);
    return (word)hash_value(v, t);
}

word ella_lib_ord(const Str *s, word line, word column, word depth) {
    UNUSED(depth);
    if (s->len != 1) {
        Buffer message = {0};
        put_ascii(&message, "ord() needs one character, not ");
        put_quoted(&message, s);
        fail_with(line, column, &message);
    }
    return s->chars[0];
}

Str *ella_lib_chr(word n, word line, word column, word depth) {
    if (n < 0 || n > 0x10FFFF || (n >= 0xD800 && n < 0xE000)) {  // surrogates aren't characters
        fail(line, column, "chr() can't make a character from %" PRId64, n);
    }
    Str *s = str_alloc(1, depth);
    s->chars[0] = (uint32_t)n;
    return s;
}

// NaN for a negative number, as in ella/library.py.
word ella_lib_sqrt(word bits, word line, word column, word depth) {
    UNUSED(line), UNUSED(column), UNUSED(depth);
    double x = as_f64(bits);
    return f64_bits(x >= 0 ? sqrt(x) : NAN);
}

word ella_lib_floor(word bits, word line, word column, word depth) {
    UNUSED(line), UNUSED(column), UNUSED(depth);
    return f64_bits(floor(as_f64(bits)));
}

word ella_lib_ceil(word bits, word line, word column, word depth) {
    UNUSED(line), UNUSED(column), UNUSED(depth);
    return f64_bits(ceil(as_f64(bits)));
}

// Halfway cases round away from zero.
word ella_lib_round(word bits, word line, word column, word depth) {
    UNUSED(line), UNUSED(column), UNUSED(depth);
    return f64_bits(round(as_f64(bits)));
}

word ella_lib_exp(word bits, word line, word column, word depth) {
    UNUSED(line), UNUSED(column), UNUSED(depth);
    return f64_bits(exp(as_f64(bits)));
}

word ella_lib_ln(word bits, word line, word column, word depth) {
    UNUSED(line), UNUSED(column), UNUSED(depth);
    return f64_bits(log(as_f64(bits)));
}

word ella_lib_sin(word bits, word line, word column, word depth) {
    UNUSED(line), UNUSED(column), UNUSED(depth);
    return f64_bits(sin(as_f64(bits)));
}

word ella_lib_cos(word bits, word line, word column, word depth) {
    UNUSED(line), UNUSED(column), UNUSED(depth);
    return f64_bits(cos(as_f64(bits)));
}

word ella_lib_tan(word bits, word line, word column, word depth) {
    UNUSED(line), UNUSED(column), UNUSED(depth);
    return f64_bits(tan(as_f64(bits)));
}

word ella_lib_asin(word bits, word line, word column, word depth) {
    UNUSED(line), UNUSED(column), UNUSED(depth);
    return f64_bits(asin(as_f64(bits)));
}

word ella_lib_acos(word bits, word line, word column, word depth) {
    UNUSED(line), UNUSED(column), UNUSED(depth);
    return f64_bits(acos(as_f64(bits)));
}

word ella_lib_atan(word bits, word line, word column, word depth) {
    UNUSED(line), UNUSED(column), UNUSED(depth);
    return f64_bits(atan(as_f64(bits)));
}

word ella_lib_atan2(word y, word x, word line, word column, word depth) {
    UNUSED(line), UNUSED(column), UNUSED(depth);
    return f64_bits(atan2(as_f64(y), as_f64(x)));
}

word ella_lib_log10(word bits, word line, word column, word depth) {
    UNUSED(line), UNUSED(column), UNUSED(depth);
    return f64_bits(log10(as_f64(bits)));
}

word ella_lib_log2(word bits, word line, word column, word depth) {
    UNUSED(line), UNUSED(column), UNUSED(depth);
    return f64_bits(log2(as_f64(bits)));
}

// upper and lower change ASCII letters only.
static Str *change_case(const Str *s, uint32_t from, word depth) {
    Str *result = str_alloc(s->len, depth);
    for (word i = 0; i < s->len; i++) {
        uint32_t c = s->chars[i];
        result->chars[i] = c >= from && c < from + 26 ? c ^ 0x20 : c;
    }
    return result;
}

Str *ella_lib_upper(const Str *s, word line, word column, word depth) {
    UNUSED(line), UNUSED(column);
    return change_case(s, 'a', depth);
}

Str *ella_lib_lower(const Str *s, word line, word column, word depth) {
    UNUSED(line), UNUSED(column);
    return change_case(s, 'A', depth);
}

word ella_lib_starts_with(const Str *s, const Str *part, word line, word column, word depth) {
    UNUSED(line), UNUSED(column), UNUSED(depth);
    return matches_at(s, 0, part);
}

word ella_lib_ends_with(const Str *s, const Str *part, word line, word column, word depth) {
    UNUSED(line), UNUSED(column), UNUSED(depth);
    return s->len >= part->len && matches_at(s, s->len - part->len, part);
}

// s with copies of fill before it (or after it) to make it width characters long.
static Str *pad(const char *name, const Str *s, word width, const Str *fill, int left, word line, word column,
                word depth) {
    if (fill->len != 1) {
        Buffer message = {0};
        put_ascii(&message, name);
        put_ascii(&message, "() needs one character to fill with, not ");
        put_quoted(&message, fill);
        fail_with(line, column, &message);
    }
    word padding = width > s->len ? width - s->len : 0;
    Str *result = str_alloc(s->len + padding, depth);
    word start = left ? padding : 0;
    memcpy(result->chars + start, s->chars, (size_t)s->len * sizeof(uint32_t));
    for (word i = 0; i < padding; i++) result->chars[left ? i : s->len + i] = fill->chars[0];
    return result;
}

Str *ella_lib_pad_left(const Str *s, word width, const Str *fill, word line, word column, word depth) {
    return pad("pad_left", s, width, fill, 1, line, column, depth);
}

Str *ella_lib_pad_right(const Str *s, word width, const Str *fill, word line, word column, word depth) {
    return pad("pad_right", s, width, fill, 0, line, column, depth);
}

#define MAX_DIGITS 100  // as in ella/library.py

// x with a fixed number of decimals, as printf (and Python's format) writes it, but NaN is always "nan".
Str *ella_lib_format_f64(word bits, word digits, word line, word column, word depth) {
    if (digits < 0 || digits > MAX_DIGITS) {
        fail(line, column, "format_f64() can write from 0 to %d decimals, not %" PRId64, MAX_DIGITS, digits);
    }
    double x = as_f64(bits);
    if (isnan(x)) x = NAN;  // not "-nan"
    int len = snprintf(NULL, 0, "%.*f", (int)digits, x);
    char *text = malloc((size_t)len + 1);
    if (text == NULL) out_of_memory("can't format a number");
    snprintf(text, (size_t)len + 1, "%.*f", (int)digits, x);
    Str *s = str_alloc(len, depth);
    for (int i = 0; i < len; i++) s->chars[i] = (unsigned char)text[i];
    free(text);
    return s;
}

word ella_lib_env(const Str *name, word line, word column, word depth) {
    if (name->len == 0) return none(depth);
    for (word i = 0; i < name->len; i++) {
        if (name->chars[i] == '=' || name->chars[i] == 0) return none(depth);
    }
    char *text = encode(name);
    const char *value = getenv(text);
    free(text);
    if (value == NULL) return none(depth);
    Str *s = decode((const unsigned char *)value, strlen(value), depth);
    if (s == NULL) {
        Buffer message = {0};
        put_ascii(&message, "env(): the value of ");
        for (word i = 0; i < name->len; i++) put(&message, name->chars[i]);
        put_ascii(&message, " isn't valid UTF-8");
        fail_with(line, column, &message);
    }
    return some((word)s, depth);
}

word ella_lib_write_file(const Str *path, const Str *s, word line, word column, word depth) {
    UNUSED(line), UNUSED(column);
    int error = 0;
    for (word i = 0; i < path->len; i++) {
        if (path->chars[i] == 0) error = EINVAL;  // as open() would say, given the whole name
    }
    if (!error) {
        char *name = encode(path);
        int fd = open(name, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0666);
        free(name);
        if (fd < 0) error = errno;
        size_t len = 0, done = 0;
        char *data = fd < 0 ? NULL : encode_len(s, &len);
        while (!error && done < len) {
            ssize_t wrote = write(fd, data + done, len - done);
            if (wrote < 0) error = errno;
            if (wrote > 0) done += (size_t)wrote;
        }
        free(data);
        if (fd >= 0 && close(fd) != 0 && !error) error = errno;
    }
    if (error) {
        Buffer message = {0};
        put_ascii(&message, "can't write ");
        put_quoted(&message, path);
        put_ascii(&message, ": ");
        put_ascii(&message, strerror(error));
        return err(&message, line, column, depth);
    }
    return ok(0, depth);  // Ok(())
}

// Stop the program at once, without running finalizers.
word ella_lib_exit(word status, word line, word column, word depth) {
    UNUSED(depth);
    if (status < 0 || status > 255) fail(line, column, "exit() needs a status from 0 to 255, not %" PRId64, status);
    fflush(stdout);
    exit((int)status);
}

// abs, min and max, for each number type.

word ella_lib_abs_i64(word n, word line, word column, word depth) {
    UNUSED(depth);
    if (n == INT64_MIN) fail(line, column, "i64 overflow: abs(%" PRId64 ") doesn't fit in 64 bits", n);
    return n < 0 ? -n : n;
}

word ella_lib_abs_f64(word bits, word line, word column, word depth) {
    UNUSED(line), UNUSED(column), UNUSED(depth);
    return f64_bits(fabs(as_f64(bits)));
}

// min(a, b) is a unless b is less, and max(a, b) a unless b is greater (so a NaN a is kept).
word ella_lib_abs_i32(word n, word line, word column, word depth) {
    UNUSED(depth);
    if (n == INT32_MIN) fail(line, column, "i32 overflow: abs(%" PRId64 ") doesn't fit in 32 bits", n);
    return n < 0 ? -n : n;
}

word ella_lib_min_i32(word a, word b, word line, word column, word depth) {
    UNUSED(line), UNUSED(column), UNUSED(depth);
    return b < a ? b : a;
}

word ella_lib_max_i32(word a, word b, word line, word column, word depth) {
    UNUSED(line), UNUSED(column), UNUSED(depth);
    return b > a ? b : a;
}

word ella_lib_min_i64(word a, word b, word line, word column, word depth) {
    UNUSED(line), UNUSED(column), UNUSED(depth);
    return b < a ? b : a;
}

word ella_lib_max_i64(word a, word b, word line, word column, word depth) {
    UNUSED(line), UNUSED(column), UNUSED(depth);
    return b > a ? b : a;
}

word ella_lib_min_f64(word a, word b, word line, word column, word depth) {
    UNUSED(line), UNUSED(column), UNUSED(depth);
    return as_f64(b) < as_f64(a) ? b : a;
}

word ella_lib_max_f64(word a, word b, word line, word column, word depth) {
    UNUSED(line), UNUSED(column), UNUSED(depth);
    return as_f64(b) > as_f64(a) ? b : a;
}

word ella_lib_abs_f32(word bits, word line, word column, word depth) {
    UNUSED(line), UNUSED(column), UNUSED(depth);
    return f32_bits(fabsf(as_f32(bits)));
}

word ella_lib_min_f32(word a, word b, word line, word column, word depth) {
    UNUSED(line), UNUSED(column), UNUSED(depth);
    return as_f32(b) < as_f32(a) ? b : a;
}

word ella_lib_max_f32(word a, word b, word line, word column, word depth) {
    UNUSED(line), UNUSED(column), UNUSED(depth);
    return as_f32(b) > as_f32(a) ? b : a;
}

// The program starts here, and runs the top-level code (generated as
// ella_main) on a stack of its own, big enough for ella_max_depth nested
// calls. Only the parts it uses take memory. A page at its end that can't
// be used stops a program that runs past it, rather than letting it
// overwrite other memory.

#define STACK_PER_CALL ((size_t)256 << 10)

extern word ella_main(char *stack);  // gives the exit status
extern const word ella_uses_avx2;  // in the generated code: whether it has AVX2 and FMA instructions (for arrays)

static int process_vm_peak(size_t *bytes) {
    FILE *status = fopen("/proc/self/status", "r");
    if (status == NULL) return 0;
    char line[256];
    size_t kib;
    while (fgets(line, sizeof(line), status)) {
        if (sscanf(line, "VmPeak: %zu kB", &kib) == 1) {
            *bytes = kib * 1024;
            fclose(status);
            return 1;
        }
    }
    int error = ferror(status) ? errno : 0;
    fclose(status);
    errno = error;
    return 0;
}

int main(int argc, char **argv) {
    __builtin_cpu_init();
    if (ella_uses_avx2 && !(__builtin_cpu_supports("avx2") && __builtin_cpu_supports("fma"))) {
        fputs("this program uses AVX2 and FMA instructions, which this CPU doesn't have\n", stderr);
        return 1;
    }
    poison = getenv("ELLA_POISON") != NULL;
    arg_count = argc - 1;
    arg_values = argv + 1;
    reserve();
    size_t stack_size = (size_t)ella_max_depth * STACK_PER_CALL + ((size_t)1 << 30);
    char *stack = mmap(NULL, stack_size, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS | MAP_NORESERVE, -1, 0);
    if (stack == MAP_FAILED || mprotect(stack, 4096, PROT_NONE) != 0) out_of_memory("can't make the stack");
    word status = ella_main(stack + stack_size);
    ella_reset(0, 1);  // the globals, and their finalizers
    fflush(stdout);
    int runtime_stats = getenv("ELLA_RUNTIME_STATS") != NULL;
    if (runtime_stats || getenv("ELLA_ARENA_STATS") != NULL) fprintf(stderr, "arena peak: %zu bytes\n", peak);
    if (runtime_stats) {
        size_t vm_peak;
        if (!process_vm_peak(&vm_peak)) {
            if (errno) fprintf(stderr, "can't read process virtual-memory peak: %s\n", strerror(errno));
            else fputs("can't find VmPeak in /proc/self/status\n", stderr);
            return 2;
        }
        fprintf(stderr, "virtual peak: %zu bytes\n", vm_peak);
        fprintf(stderr, "bytes copied: %zu bytes\n", copied_bytes);
    }
    return (int)status;
}
