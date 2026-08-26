package main

import (
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"strings"
	"testing"
)

func TestWindowsReplaceUsesAtomicReplacePrimitiveWithoutDeletingDestination(t *testing.T) {
	source, err := os.ReadFile("atomic_replace_windows.go")
	if err != nil {
		t.Fatal(err)
	}
	file, err := parser.ParseFile(token.NewFileSet(), "atomic_replace_windows.go", source, 0)
	if err != nil {
		t.Fatal(err)
	}

	var moveFileEx, removesDestination bool
	ast.Inspect(file, func(node ast.Node) bool {
		call, ok := node.(*ast.CallExpr)
		if !ok {
			return true
		}
		selector, ok := call.Fun.(*ast.SelectorExpr)
		if !ok {
			return true
		}
		pkg, ok := selector.X.(*ast.Ident)
		if !ok {
			return true
		}
		moveFileEx = moveFileEx || pkg.Name == "windows" && selector.Sel.Name == "MoveFileEx"
		removesDestination = removesDestination || pkg.Name == "os" && selector.Sel.Name == "Remove"
		return true
	})

	if !moveFileEx {
		t.Fatal("replaceFile does not call windows.MoveFileEx")
	}
	if removesDestination {
		t.Fatal("replaceFile deletes destination before replacement")
	}
	text := string(source)
	if !containsAll(text, "windows.MOVEFILE_REPLACE_EXISTING", "windows.MOVEFILE_WRITE_THROUGH") {
		t.Fatal("replaceFile does not request replacement and write-through semantics")
	}
}

func TestWindowsDirectoryDurabilityReliesOnMoveFileExWriteThrough(t *testing.T) {
	source, err := os.ReadFile("atomic_replace_windows.go")
	if err != nil {
		t.Fatal(err)
	}
	file, err := parser.ParseFile(token.NewFileSet(), "atomic_replace_windows.go", source, 0)
	if err != nil {
		t.Fatal(err)
	}

	var syncParentDirectory, opensDirectory bool
	for _, declaration := range file.Decls {
		function, ok := declaration.(*ast.FuncDecl)
		if ok && function.Name.Name == "syncParentDirectory" {
			syncParentDirectory = true
		}
	}
	ast.Inspect(file, func(node ast.Node) bool {
		call, ok := node.(*ast.CallExpr)
		if !ok {
			return true
		}
		selector, ok := call.Fun.(*ast.SelectorExpr)
		if !ok {
			return true
		}
		pkg, ok := selector.X.(*ast.Ident)
		if !ok {
			return true
		}
		opensDirectory = opensDirectory || pkg.Name == "os" && selector.Sel.Name == "Open"
		return true
	})
	if !syncParentDirectory {
		t.Fatal("Windows source does not define syncParentDirectory")
	}
	if opensDirectory {
		t.Fatal("Windows directory durability must not use os.Open(dir).Sync")
	}
}

func containsAll(text string, values ...string) bool {
	for _, value := range values {
		if !strings.Contains(text, value) {
			return false
		}
	}
	return true
}
