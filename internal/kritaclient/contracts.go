package kritaclient

import "maps"

var (
	text          = &Schema{Type: "string"}
	nonempty      = &Schema{Type: "string", MinLength: 1}
	color         = &Schema{Type: "string", Pattern: `^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$`}
	integer       = &Schema{Type: "integer"}
	positiveInt   = &Schema{Type: "integer", ExclusiveMinimum: pointer(0.0)}
	positive      = &Schema{Type: "number", ExclusiveMinimum: pointer(0.0)}
	unit          = &Schema{Type: "number", Minimum: pointer(0.0), Maximum: pointer(1.0)}
	coordinate    = &Schema{Type: "number", Minimum: pointer(-1e9), Maximum: pointer(1e9)}
	flag          = &Schema{Type: "boolean"}
	blend         = enumeration("normal", "multiply", "screen", "add", "overlay")
	pair          = &Schema{Type: "array", PrefixItems: []*Schema{coordinate, coordinate}, MinItems: 2, MaxItems: 2}
	triple        = &Schema{Type: "array", PrefixItems: []*Schema{coordinate, coordinate, unit}, MinItems: 3, MaxItems: 3}
	strokePoints  = &Schema{Type: "array", Items: &Schema{AnyOf: []*Schema{pair, triple}}, MinItems: 2, MaxItems: 4096}
	polygonPoints = &Schema{Type: "array", Items: pair, MinItems: 3, MaxItems: 4096}
	polygonFields = map[string]*Schema{
		"points": polygonPoints, "color": nullable(color), "smooth": defaulted(flag, "true"),
		"feather":        defaulted(&Schema{Type: "number", Minimum: pointer(0.0), Maximum: pointer(256.0)}, "0.0"),
		"preserve_alpha": defaulted(flag, "false"),
	}
	actionSchemas = makeContracts()
)

func makeContracts() map[string]*Schema {
	gradient := maps.Clone(polygonFields)
	gradient["color"] = color
	gradient["gradient_end_color"] = color
	gradient["gradient_type"] = defaulted(enumeration("linear", "radial"), `"linear"`)
	gradient["direction"] = defaulted(coordinate, "0.0")
	rect := object(map[string]*Schema{"x": integer, "y": integer, "width": positiveInt, "height": positiveInt}, "x", "y", "width", "height")
	return map[string]*Schema{
		"new_canvas": object(map[string]*Schema{
			"width": defaulted(positiveInt, "800"), "height": defaulted(positiveInt, "600"),
			"name": defaulted(text, `"New Canvas"`), "background": defaulted(color, `"#1a1a2e"`),
		}),
		"set_color": object(map[string]*Schema{"color": color}, "color"),
		"set_brush": object(map[string]*Schema{"preset": nullable(nonempty), "size": nullable(positiveInt), "opacity": nullable(unit)}),
		"stroke": object(map[string]*Schema{
			"points": strokePoints, "pressure": defaulted(unit, "1.0"), "size": nullable(positive),
			"color": nullable(color), "opacity": nullable(unit), "smooth": defaulted(flag, "true"),
			"curve": nullable(enumeration("linear", "bezier")), "taper_start": defaulted(unit, "0.0"),
			"taper_end": defaulted(unit, "0.0"), "preserve_alpha": defaulted(flag, "false"),
		}, "points"),
		"fill": object(map[string]*Schema{"x": integer, "y": integer, "radius": defaulted(positiveInt, "50")}, "x", "y"),
		"draw_shape": object(map[string]*Schema{
			"shape": enumeration("rectangle", "ellipse", "line"), "x": integer, "y": integer,
			"width": defaulted(positiveInt, "100"), "height": defaulted(positiveInt, "100"),
			"fill": defaulted(flag, "true"), "stroke": defaulted(flag, "false"),
			"x2": nullable(integer), "y2": nullable(integer), "line_width": defaulted(positive, "2.0"),
		}, "shape", "x", "y"),
		"get_canvas": object(map[string]*Schema{
			"filename": defaulted(nonempty, `"canvas.png"`), "show_grid": defaulted(flag, "false"),
			"grid_spacing":        defaulted(&Schema{Type: "integer", Minimum: pointer(8.0), Maximum: pointer(32768.0)}, "100"),
			"diff_with_reference": nullable(nonempty),
		}),
		"undo": object(nil), "redo": object(nil), "list_layers": object(nil),
		"deselect": object(nil), "get_current_brush": object(nil),
		"clear":        object(map[string]*Schema{"color": defaulted(color, `"#1a1a2e"`)}),
		"save":         object(map[string]*Schema{"path": nonempty}, "path"),
		"open_file":    object(map[string]*Schema{"path": nonempty}, "path"),
		"get_color_at": object(map[string]*Schema{"x": integer, "y": integer}, "x", "y"),
		"list_brushes": object(map[string]*Schema{
			"filter": defaulted(text, `""`), "limit": defaulted(&Schema{Type: "integer", Minimum: pointer(1.0), Maximum: pointer(10000.0)}, "20"),
		}),
		"fill_polygon":     object(polygonFields, "points"),
		"gradient_polygon": object(gradient, "points", "color", "gradient_end_color"),
		"create_layer":     object(map[string]*Schema{"name": defaulted(nonempty, `"Paint Layer"`), "blending_mode": defaulted(blend, `"normal"`)}),
		"set_layer_properties": object(map[string]*Schema{
			"layer_id": nullable(nonempty), "name": nullable(nonempty), "opacity": nullable(unit),
			"visible": nullable(flag), "blending_mode": nullable(blend), "preserve_alpha": nullable(flag),
		}),
		"set_active_layer": object(map[string]*Schema{"layer_id": nonempty}, "layer_id"),
		"delete_layer":     object(map[string]*Schema{"layer_id": nonempty}, "layer_id"),
		"merge_down":       object(map[string]*Schema{"layer_id": nonempty}, "layer_id"),
		"select_rect":      rect, "select_ellipse": rect,
		"select_polygon":  object(map[string]*Schema{"points": polygonPoints, "smooth": defaulted(flag, "false")}, "points"),
		"select_by_alpha": object(map[string]*Schema{"threshold": defaulted(&Schema{Type: "integer", Minimum: pointer(1.0), Maximum: pointer(255.0)}, "1")}),
		"fill_selection":  object(map[string]*Schema{"color": nullable(color), "preserve_alpha": defaulted(flag, "false")}),
		"batch": object(map[string]*Schema{"commands": {Type: "array", MinItems: 1, Items: object(map[string]*Schema{
			"action": nonempty, "params": {Type: "object"},
		}, "action", "params")}}, "commands"),
	}
}

func InputSchema(action string) *Schema {
	schema := actionSchemas[action]
	if action == "health" {
		return object(nil)
	}
	copy := *schema
	copy.Properties = maps.Clone(schema.Properties)
	for _, name := range toolTextFields[action] {
		field := *copy.Properties[name]
		if len(field.AnyOf) > 0 {
			field = *nullable(text)
		} else {
			field.MinLength = 0
		}
		copy.Properties[name] = &field
	}
	switch action {
	case "draw_shape":
		delete(copy.Properties, "line_width")
	case "list_brushes":
		copy.Properties["limit"] = defaulted(positiveInt, "20")
	}
	return &copy
}

var toolTextFields = map[string][]string{
	"set_brush": {"preset"}, "save": {"path"}, "open_file": {"path"},
	"get_canvas": {"filename", "diff_with_reference"}, "create_layer": {"name"},
	"set_layer_properties": {"layer_id", "name"}, "set_active_layer": {"layer_id"},
}
