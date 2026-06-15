"""Synthetic objdump-style assembly text for unit testing."""

SINGLE_FUNCTION_ASM = """\
00008000 <main>:
    8000:	e92d4800 	push	{fp, lr}
    8004:	e28db004 	add	fp, sp, #4
    8008:	e3a00000 	mov	r0, #0
    800c:	e24bd004 	sub	sp, fp, #4
    8010:	e8bd8800 	pop	{fp, pc}
"""

MULTI_FUNCTION_ASM = """\
00008000 <init_hardware>:
    8000:	e92d4800 	push	{fp, lr}
    8004:	e28db004 	add	fp, sp, #4

00008100 <process_data>:
    8100:	e92d4800 	push	{fp, lr}
    8104:	e28db004 	add	fp, sp, #4
    8108:	e3a00001 	mov	r0, #1

00008200 <cleanup>:
    8200:	e92d4800 	push	{fp, lr}
    8204:	e24bd004 	sub	sp, fp, #4
"""

EMPTY_BODY_ASM = """\
00008000 <empty_func>:

00008100 <real_func>:
    8100:	e92d4800 	push	{fp, lr}
"""

COMPLEX_NAMES_ASM = """\
00008000 <std::vector<int>::push_back(int const&)>:
    8000:	e92d4800 	push	{fp, lr}
    8004:	e28db004 	add	fp, sp, #4

00008100 <ns::MyClass<T>::~MyClass()>:
    8100:	e92d4800 	push	{fp, lr}
"""
