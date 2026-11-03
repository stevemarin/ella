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

// A string, as Unicode code points, so indexing and len() are simple.
typedef struct {
    word len;
    uint32_t chars[];
} Str;

// A list or a tuple: a length and that many words. A list may have room
// for more items after them: `cap` is how many fit. (A tuple's is its length.)
typedef struct {
    word len;
    word cap;
    word items[];
} List;

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

// Type descriptors, emitted as static data by the code generator. Equality
// and the text form of a list of enums of floats need to know all of that.
enum Kind { K_UNIT, K_I64, K_F64, K_BOOL, K_STR, K_RANGE, K_LIST, K_TUPLE, K_FUNCTION, K_ENUM, K_POOL, K_HANDLE, K_CODE };

struct Type {
    word kind;
    word count;  // tuple items or enum variants
    // list: parts[0] is the element type; tuple: the item types;
    // enum: VariantType descriptors.
    const void *parts[];
};

// A variant (or struct). A nocopy struct may have a finalizer, a closure run
// on each value when it dies.
typedef struct {
    const char *name;
    word count;
    const Closure *finalize;
    const Type *fields[];
} VariantType;

// How many Ella calls are in progress, for the stack overflow check. The
// top-level code itself counts as one, as in the interpreters.
word ella_depth = 1;

// Errors

// Stop the program with a run-time error: "line:column: message", after
// everything the program printed so far.
_Noreturn static void fail(word line, word column, const char *format, ...) {
    fflush(stdout);
    fprintf(stderr, "%" PRId64 ":%" PRId64 ": ", line, column);
    va_list args;
    va_start(args, format);
    vfprintf(stderr, format, args);
    va_end(args);
    fputc('\n', stderr);
    exit(1);
}

_Noreturn void ella_error(word line, word column, const char *message) { fail(line, column, "%s", message); }

_Noreturn void ella_overflow(word a, word b, const char *op, word line, word column) {
    fail(line, column, "i64 overflow: %" PRId64 " %s %" PRId64 " doesn't fit in 64 bits", a, op, b);
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
// numbered from SLOT_DEPTH up, which is freed when the value is.
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
#define KEEP_SCOPE ((size_t)16 << 20)  // when freeing, give memory beyond this back to the OS
#define KEEP_SLOT ((size_t)64 << 10)

// A value with a finalizer, in the arena it lives in: a nocopy struct with
// its finalizer, or a pool (with no closure), which frees its values.
typedef struct {
    word object;
    const Closure *finalize;
} Final;

typedef struct {
    char *top;
    Final *finals;
    word count, capacity;
} Arena;

static char *space;  // all the arenas' address space
static word scope_count;  // how many scope arenas there are
static size_t scope_size;  // and the address space of each
static Arena *scope_arenas;
static Arena *slot_arenas;
static word *free_slots, free_slot_count, slot_arenas_used;

// The first depth not taken by a call in progress (depth 0 is global).
word ella_next_depth = 1;

// With ELLA_POISON set, freed memory is overwritten, so that reading an
// object after its arena is freed (a bug in the compiler) shows up as
// nonsense rather than going unnoticed. The tests set it.
static int poison;

// With ELLA_ARENA_STATS set, the program reports the most memory its arenas
// ever held at once when it finishes.
static size_t in_use, peak;

static size_t round16(size_t size) { return (size + 15) & ~(size_t)15; }

extern const word ella_max_depth;  // in the generated code: how deeply calls may nest

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
    for (word i = 0; i < scope_count; i++) scope_arenas[i].top = space + i * scope_size;
}

static char *arena_start(word depth) {
    if (depth >= SLOT_DEPTH) return space + scope_count * scope_size + (size_t)(depth - SLOT_DEPTH) * SLOT_SIZE;
    return space + depth * scope_size;
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
    size_t limit = depth >= SLOT_DEPTH ? SLOT_SIZE : scope_size;
    if ((size_t)(object + round16(size) - arena_start(depth)) > limit) out_of_memory("an arena is full");
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

extern void ella_call1(const Closure *closure, word argument);  // in the generated code
static void free_pool(word pool);

static void free_arena(word depth) {
    Arena *a = arena(depth);
    // Finalizers first, newest first, while the values are still there.
    while (a->count > 0) {
        Final final = a->finals[--a->count];
        if (final.finalize == NULL) {
            free_pool(final.object);
        } else {
            ella_call1(final.finalize, final.object);
        }
    }
    char *start = arena_start(depth);
    size_t used = (size_t)(a->top - start);
    in_use -= used;
    if (poison) memset(start, 0xA5, used);
    a->top = start;
    // Memory well beyond what the arena usually needs goes back to the OS.
    // It reads as zeros if it is used again.
    size_t keep = depth >= SLOT_DEPTH ? KEEP_SLOT : KEEP_SCOPE;
    if (used > keep) madvise(start + keep, used - keep, MADV_DONTNEED);
}

// Free the arenas at depths end - 1, ..., first: a loop's levels when an
// iteration ends.
void ella_reset(word first, word end) {
    for (word depth = end - 1; depth >= first; depth--) free_arena(depth);
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

word ella_pow_f64(word a_bits, word b_bits) {
    double a = as_f64(a_bits), b = as_f64(b_bits);
    if (a == 0.0 && b < 0.0) return f64_bits(INFINITY);  // as ella.operators.float_pow
    return f64_bits(pow(a, b));
}

// Strings

static Str *str_alloc(word len, word depth) {
    Str *s = alloc(sizeof(Str) + (size_t)len * sizeof(uint32_t), depth);
    s->len = len;
    return s;
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

// Lists (and tuples, which have the same layout)

List *ella_list_alloc(word len, word depth) {
    List *xs = alloc(sizeof(List) + (size_t)len * sizeof(word), depth);
    xs->len = xs->cap = len;
    return xs;
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
    return t->kind != K_UNIT && t->kind != K_I64 && t->kind != K_F64 && t->kind != K_BOOL && t->kind != K_HANDLE;
}

word ella_copy(word v, const Type *t, word depth) {
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
    case K_LIST:
    case K_TUPLE: {
        const List *xs = (const List *)v;
        List *copy = ella_list_alloc(xs->len, depth);
        for (word i = 0; i < xs->len; i++) {
            const Type *item = t->kind == K_LIST ? t->parts[0] : t->parts[i];
            copy->items[i] = is_boxed(item) ? ella_copy(xs->items[i], item, depth) : xs->items[i];
        }
        return (word)copy;
    }
    case K_ENUM: {
        const Variant *x = (const Variant *)v;
        const VariantType *variant = t->parts[x->tag];
        Variant *copy = ella_variant_alloc(x->tag, x->count, depth);
        for (word i = 0; i < x->count; i++) {
            const Type *field = variant->fields[i];
            copy->fields[i] = is_boxed(field) ? ella_copy(x->fields[i], field, depth) : x->fields[i];
        }
        return (word)copy;
    }
    case K_FUNCTION: {
        const Closure *c = (const Closure *)v;
        Closure *copy = ella_closure_alloc(c->code, c->count, c->types, depth);
        for (word i = 0; i < c->count; i++) {
            const Type *item = c->types->parts[i];
            copy->captured[i] = is_boxed(item) ? ella_copy(c->captured[i], item, depth) : c->captured[i];
        }
        return (word)copy;
    }
    default:  // (), i64, f64, bool, handles; pools can't be copied
        return v;
    }
}

// `move v @r`: a value that can't be copied, moved to another arena: copied
// there, with the finalizers of the values it holds moving with them. The
// old value is never used again.
word ella_move(word v, const Type *t, word depth) {
    switch (t->kind) {
    case K_TUPLE: {
        const List *xs = (const List *)v;
        List *moved = ella_list_alloc(xs->len, depth);
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
        word *moved = alloc(5 * sizeof(word), depth);
        memcpy(moved, (const void *)v, 5 * sizeof(word));
        unregister(v);
        ella_register((word)moved, NULL, depth);
        return (word)moved;
    }
    default:
        return is_boxed(t) ? ella_copy(v, t, depth) : v;
    }
}

// Storing `value` into an object that may be a pool's value (`@slot`): a
// pool's value lives in its slot's arena, so the value is copied there.
word ella_store_slot(word object, word value, const Type *t) {
    word depth = ella_region_of(object);
    if (depth < SLOT_DEPTH || !is_boxed(t)) return value;
    return ella_copy(value, t, depth);
}

// Pools. A pool has slots, each holding a value (in its own slot arena) or
// free. A slot's generation goes up when its value is freed, and a handle
// remembers the generation it was made with, so a handle to a freed value
// is caught. A handle is one word: the slot in the low 32 bits, the
// generation in the high 32.

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
} Pool;

word ella_pool_new(word depth) {
    Pool *pool = alloc(sizeof(Pool), depth);
    *pool = (Pool){0, 0, 0, NULL, -1};
    ella_register((word)pool, NULL, depth);  // when it dies, its values are freed
    return (word)pool;
}

static word handle(word slot, word generation) { return (word)(((uint64_t)generation << 32) | (uint64_t)slot); }

word ella_alloc(Pool *pool, word value, const Type *t) {
    word slot;
    if (pool->free >= 0) {
        slot = pool->free;
        pool->free = pool->slots[slot].next_free;
    } else {
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
    return handle(slot, s->generation);
}

static Slot *slot_of(Pool *pool, word h, word line, word column) {
    word slot = (word)((uint64_t)h & 0xFFFFFFFF), generation = (word)((uint64_t)h >> 32);
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
    s->generation++;
    s->next_free = pool->free;
    pool->free = slot;
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

// `xs += ys`: add copies of ys's items to xs, which lives in the arena at
// `depth`, and return xs, which may have moved. If xs is the last object in
// its arena, it grows in place; otherwise it moves to a new home with room
// for twice as many items. Either way, adding an item costs O(1) on average.
List *ella_list_append(List *xs, const List *ys, const Type *item, word depth) {
    if (depth < 0) depth = ella_region_of((word)xs);  // @slot: the arena xs is in
    word n = ys->len, len = xs->len;  // ys may be xs, so read its length first
    if (len + n > xs->cap) {
        word cap = len + n > 2 * xs->cap ? len + n : 2 * xs->cap;
        if (cap < 4) cap = 4;
        Arena *a = arena(depth);
        char *end = (char *)xs + round16(sizeof(List) + (size_t)xs->cap * sizeof(word));
        if (end == a->top) {
            char *new_end = (char *)xs + round16(sizeof(List) + (size_t)cap * sizeof(word));
            size_t limit = depth >= SLOT_DEPTH ? SLOT_SIZE : scope_size;
            if ((size_t)(new_end - arena_start(depth)) > limit) out_of_memory("an arena is full");
            in_use += (size_t)(new_end - end);
            if (in_use > peak) peak = in_use;
            a->top = new_end;
            xs->cap = cap;
        } else {
            List *moved = alloc(sizeof(List) + (size_t)cap * sizeof(word), depth);
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
        const List *x = (const List *)a, *y = (const List *)b;
        for (word i = 0; i < t->count; i++) {
            if (!ella_equal(x->items[i], y->items[i], t->parts[i])) return 0;
        }
        return 1;
    }
    case K_ENUM: {
        const Variant *x = (const Variant *)a, *y = (const Variant *)b;
        if (x->tag != y->tag) return 0;
        const VariantType *variant = t->parts[x->tag];
        for (word i = 0; i < variant->count; i++) {
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

static void put_value(Buffer *b, word v, const Type *t, int nested) {
    switch (t->kind) {
    case K_UNIT: put_ascii(b, "()"); break;
    case K_I64: put_i128(b, v); break;
    case K_F64: put_f64(b, as_f64(v)); break;
    case K_BOOL: put_ascii(b, v ? "true" : "false"); break;
    case K_FUNCTION: put_ascii(b, "<function>"); break;
    case K_CODE: put_ascii(b, "<code>"); break;  // never made: native code can't build code at run time
    case K_POOL:
        put_ascii(b, "<pool of ");
        put_i128(b, ((const Pool *)v)->live);
        put(b, '>');
        break;
    case K_HANDLE:
        put_ascii(b, "<handle ");
        put_i128(b, (word)((uint64_t)v & 0xFFFFFFFF));
        put(b, ':');
        put_i128(b, (word)((uint64_t)v >> 32));
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
            put_value(b, xs->items[i], t->parts[0], 1);
        }
        put(b, ']');
        break;
    }
    case K_TUPLE: {
        const List *xs = (const List *)v;
        put(b, '(');
        for (word i = 0; i < t->count; i++) {
            if (i) put_ascii(b, ", ");
            put_value(b, xs->items[i], t->parts[i], 1);
        }
        if (t->count == 1) put(b, ',');
        put(b, ')');
        break;
    }
    case K_ENUM: {
        const Variant *x = (const Variant *)v;
        const VariantType *variant = t->parts[x->tag];
        put_ascii(b, variant->name);
        if (variant->count) {
            put(b, '(');
            for (word i = 0; i < variant->count; i++) {
                if (i) put_ascii(b, ", ");
                put_value(b, x->fields[i], variant->fields[i], 1);
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
    put_value(&b, v, t, 0);
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
    put_value(&b, v, t, 0);
    write_utf8(stdout, b.chars, b.len);
    fputc('\n', stdout);
    free(b.chars);
}

// `eprint`: the same on standard error, after what was printed so far.
void ella_eprint(word v, const Type *t) {
    fflush(stdout);
    Buffer b = {0};
    put_value(&b, v, t, 0);
    write_utf8(stderr, b.chars, b.len);
    fputc('\n', stderr);
    free(b.chars);
}

// Stop with a run-time error whose message is in a buffer.
_Noreturn static void fail_with(word line, word column, Buffer *message) {
    fflush(stdout);
    fprintf(stderr, "%" PRId64 ":%" PRId64 ": ", line, column);
    write_utf8(stderr, message->chars, message->len);
    fputc('\n', stderr);
    exit(1);
}

// A trap whose message shows a value: "no case matches {}".
_Noreturn void ella_trap_value(const char *message, word v, const Type *t, word line, word column) {
    Buffer b = {0};
    put_value(&b, v, t, 1);
    const char *hole = strstr(message, "{}");
    fflush(stdout);
    fprintf(stderr, "%" PRId64 ":%" PRId64 ": %.*s", line, column, (int)(hole - message), message);
    write_utf8(stderr, b.chars, b.len);
    fprintf(stderr, "%s\n", hole + 2);
    exit(1);
}

// Slices: `xs[a..b]` and `s[a..b]`, new lists (of copies of the items) and strings.

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
    return substring(s, r->start, (word)r->stop, depth);
}

List *ella_list_slice(const List *xs, const Range *r, const Type *item, word line, word column, word depth) {
    check_slice(r, xs->len, "list", line, column);
    List *part = ella_list_alloc((word)r->stop - r->start, depth);
    for (word i = 0; i < part->len; i++) {
        word x = xs->items[r->start + i];
        part->items[i] = is_boxed(item) ? ella_copy(x, item, depth) : x;
    }
    return part;
}

// The library: built-in functions with fixed types, which must behave
// exactly like ella/library.py. Each takes its arguments, then the line and
// column of the call (for errors), then the depth of the arena for its
// result.

#define UNUSED(x) (void)(x)

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

// Err(Error(message)), with the message in a buffer, which is freed.
static word err(Buffer *message, word depth) {
    return variant1(TAG_ERR, variant1(TAG_ERROR, (word)buffer_str(message, depth), depth), depth);
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

word ella_lib_to_f64(word n, word line, word column, word depth) {
    UNUSED(line), UNUSED(column), UNUSED(depth);
    return f64_bits((double)n);
}

word ella_lib_to_i64(word bits, word line, word column, word depth) {
    UNUSED(depth);
    double x = as_f64(bits), whole = trunc(x);
    if (!(whole >= -9223372036854775808.0 && whole < 9223372036854775808.0)) {  // also false for NaN
        Buffer b = {0};
        put_f64(&b, x);
        char text[40];
        for (word i = 0; i < b.len; i++) text[i] = (char)b.chars[i];
        text[b.len] = 0;
        free(b.chars);
        fail(line, column, "to_i64() can't convert %s to an i64", text);
    }
    return (word)whole;
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
    return err(&message, depth);
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
        return err(&message, depth);
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
        return err(&message, depth);
    }
    Str *s = decode(data, len, depth);
    free(data);
    if (s == NULL) {
        put_ascii(&message, "it isn't valid UTF-8");
        return err(&message, depth);
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
            parts->items[k++] = (word)substring(s, start, i, depth);
            i += separator->len;
            start = i;
        } else {
            i++;
        }
    }
    parts->items[k] = (word)substring(s, start, s->len, depth);
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
    return substring(s, start, stop, depth);
}

List *ella_lib_chars(const Str *s, word line, word column, word depth) {
    UNUSED(line), UNUSED(column);
    List *chars = ella_list_alloc(s->len, depth);
    for (word i = 0; i < s->len; i++) chars->items[i] = (word)substring(s, i, i + 1, depth);
    return chars;
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
        return err(&message, depth);
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

// The program starts here, and runs the top-level code (generated as
// ella_main) on a stack of its own, big enough for ella_max_depth nested
// calls. Only the parts it uses take memory. A page at its end that can't
// be used stops a program that runs past it, rather than letting it
// overwrite other memory.

#define STACK_PER_CALL ((size_t)256 << 10)

extern word ella_main(char *stack);  // gives the exit status

int main(int argc, char **argv) {
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
    if (getenv("ELLA_ARENA_STATS") != NULL) fprintf(stderr, "arena peak: %zu bytes\n", peak);
    return (int)status;
}
