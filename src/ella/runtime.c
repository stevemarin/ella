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
// below. Memory is allocated and never freed: managing it properly is stage 6.

#include <inttypes.h>
#include <math.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef int64_t word;

// A string, as Unicode code points, so indexing and len() are simple.
typedef struct {
    word len;
    uint32_t chars[];
} Str;

// A list or a tuple: a length and that many words.
typedef struct {
    word len;
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

// A function value: the code to run and the values it captured.
typedef struct {
    void *code;
    word count;
    word captured[];
} Closure;

// Type descriptors, emitted as static data by the code generator. Equality
// and the text form of a list of enums of floats need to know all of that.
enum Kind { K_UNIT, K_I64, K_F64, K_BOOL, K_STR, K_RANGE, K_LIST, K_TUPLE, K_FUNCTION, K_ENUM };

typedef struct Type {
    word kind;
    word count;  // tuple items or enum variants
    // list: parts[0] is the element type; tuple: the item types;
    // enum: VariantType descriptors.
    const void *parts[];
} Type;

typedef struct {
    const char *name;
    word count;
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

static void *alloc(size_t size) {
    void *memory = malloc(size ? size : 1);
    if (memory == NULL) {
        fputs("out of memory\n", stderr);
        exit(2);
    }
    return memory;
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

Str *ella_str_alloc(word len) {
    Str *s = alloc(sizeof(Str) + (size_t)len * sizeof(uint32_t));
    s->len = len;
    return s;
}

Str *ella_str_concat(const Str *a, const Str *b) {
    Str *s = ella_str_alloc(a->len + b->len);
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

Str *ella_str_index(const Str *s, word i, word line, word column) {
    if (i < 0 || i >= s->len)
        fail(line, column, "index %" PRId64 " is out of range for a str of length %" PRId64, i, s->len);
    Str *c = ella_str_alloc(1);
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

List *ella_list_alloc(word len) {
    List *xs = alloc(sizeof(List) + (size_t)len * sizeof(word));
    xs->len = len;
    return xs;
}

List *ella_list_concat(const List *a, const List *b) {
    List *xs = ella_list_alloc(a->len + b->len);
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

// Lists are values: setting an item makes a new list.
List *ella_list_set(const List *xs, word i, word value, word line, word column) {
    check_index(xs, i, line, column);
    List *copy = ella_list_alloc(xs->len);
    memcpy(copy->items, xs->items, (size_t)xs->len * sizeof(word));
    copy->items[i] = value;
    return copy;
}

// Ranges

Range *ella_range(word start, word stop) {
    Range *r = alloc(sizeof(Range));
    r->start = start;
    r->stop = stop;
    return r;
}

Range *ella_range_incl(word start, word last) {
    Range *r = ella_range(start, last);
    r->stop = (__int128)last + 1;
    return r;
}

word ella_range_len(const Range *r) { return r->stop > r->start ? (word)(r->stop - r->start) : 0; }

word ella_range_index(const Range *r, word i) { return r->start + i; }

word ella_range_contains(const Range *r, word x) { return x >= r->start && x < r->stop; }

// Enums and closures

Variant *ella_variant_alloc(word tag, word count) {
    Variant *v = alloc(sizeof(Variant) + (size_t)count * sizeof(word));
    v->tag = tag;
    v->count = count;
    return v;
}

Closure *ella_closure_alloc(void *code, word count) {
    Closure *c = alloc(sizeof(Closure) + (size_t)count * sizeof(word));
    c->code = code;
    c->count = count;
    return c;
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

static void put(Buffer *b, uint32_t c) {
    if (b->len == b->capacity) {
        b->capacity = b->capacity ? b->capacity * 2 : 32;
        uint32_t *bigger = alloc((size_t)b->capacity * sizeof(uint32_t));
        if (b->len) memcpy(bigger, b->chars, (size_t)b->len * sizeof(uint32_t));
        b->chars = bigger;  // the old buffer is never freed, like everything else
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

Str *ella_to_text(word v, const Type *t) {
    Buffer b = {0};
    put_value(&b, v, t, 0);
    Str *s = ella_str_alloc(b.len);
    if (b.len) memcpy(s->chars, b.chars, (size_t)b.len * sizeof(uint32_t));
    return s;
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

// The program starts here, and runs the top-level code (generated as ella_main).

extern void ella_main(void);

int main(void) {
    ella_main();
    fflush(stdout);
    return 0;
}
