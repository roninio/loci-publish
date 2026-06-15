"""C++ code snippets for preflight_check testing."""

USE_AFTER_FREE_CODE = """\
void bad() {
    int* p = new int(42);
    delete p;
    p->x = 10;
}
"""

DOUBLE_FREE_CODE = """\
void bad() {
    int* p = (int*)malloc(sizeof(int));
    free(p);
    free(p);
}
"""

RECURSION_NO_GUARD_CODE = """\
int factorial(int n) {
    return n * factorial(n - 1);
}
"""

RECURSION_WITH_GUARD_CODE = """\
int factorial(int n) {
    if (n <= 1) return 1;
    return n * factorial(n - 1);
}
"""

SHIFT_OVERFLOW_CODE = """\
void bad() {
    int x = 1 << 32;
}
"""

SHIFT_SAFE_CODE = """\
void safe() {
    int x = 1 << 8;
}
"""

UNSIGNED_SUB_CODE = """\
void bad() {
    foo(size_t(len) - 1);
}
"""

SIGNED_OVERFLOW_CODE = """\
void bad() {
    int x = a * b;
}
"""

SIGNED_UNSIGNED_COMPARE_CODE = """\
void bad() {
    int x = 5;
    if (int y == size_t z) {}
}
"""

RETURN_ADDR_OF_LOCAL_CODE = """\
int* bad() {
    int local = 42;
    return &local;
}
"""

POST_MOVE_USE_CODE = """\
void bad() {
    std::string x = "hello";
    auto y = std::move(x);
    x.size();
}
"""

STATIC_INIT_CODE = """\
void bad() {
    static Foo f = Other::make();
}
"""

CLEAN_CODE = """\
int safe(int a, int b) {
    return a + b;
}
"""
