"""Synthetic CSV strings for symmap and blocks testing."""

SYMMAP_CSV = """\
name,long_name,start_address,size,namespace
main,main(),0x8000,64,global
init,ns::init(int),0x8100,32,ns
helper,helper(void),0x8200,16,global
"""

SYMMAP_CSV_EMPTY = """\
name,long_name,start_address,size,namespace
"""

SYMMAP_CSV_BAD_SIZE = """\
name,long_name,start_address,size,namespace
broken,broken(),0x9000,???,global
"""

BLOCKS_CSV = """\
s1.name,s1.long_name,r.from_addr,r.to_addr,r.asm,db.block_ids,r.src_location
main,main(),0x8000,0x8010,push {fp; lr},blk_1,main.c:10
main,main(),0x8010,0x8020,mov r0; #0,blk_2,main.c:11
init,ns::init(int),0x8100,0x8110,push {fp; lr},blk_3,init.c:5
"""

BLOCKS_CSV_EMPTY_ASM = """\
s1.name,s1.long_name,r.from_addr,r.to_addr,r.asm,db.block_ids,r.src_location
main,main(),0x8000,0x8010,,blk_1,main.c:10
main,main(),0x8010,0x8020,mov r0; #0,blk_2,main.c:11
"""
