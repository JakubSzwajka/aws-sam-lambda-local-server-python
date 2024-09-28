from pydantic import BaseModel, ValidationError
import importlib
import os
from flask import Flask, Response, jsonify, request
from typing import Dict, Any, List
import importlib.util
import sys
import yaml

app = Flask(__name__)


class LambdaContext:
    def __init__(self, resource_name="example_resource"):
        self.function_name = resource_name
        self.function_version = "$LATEST"
        self.invoked_function_arn = (
            f"arn:aws:lambda:us-east-1:123456789012:function:{resource_name}"
        )
        self.memory_limit_in_mb = 128
        self.aws_request_id = "00000000-0000-0000-0000-000000000000"
        self.log_group_name = f"/aws/lambda/{resource_name}"
        self.log_stream_name = "2023/05/14/[$LATEST]58419525dade4d17a495dceeeed55c15"
        self.get_remaining_time_in_millis = lambda: 300000  # 5 minutes


class CloudFormationLoader(yaml.SafeLoader):
    def __init__(self, stream):
        self._root = os.path.split(stream.name)[0]  # type: ignore
        super(CloudFormationLoader, self).__init__(stream)

    def include(self, node):
        filename = os.path.join(self._root, self.construct_scalar(node))  # type: ignore
        with open(filename, "r") as f:
            return yaml.load(f, CloudFormationLoader)


def construct_getatt(loader, node):
    if isinstance(node, yaml.ScalarNode):
        return {"Fn::GetAtt": loader.construct_scalar(node).split(".")}
    elif isinstance(node, yaml.SequenceNode):
        return {"Fn::GetAtt": loader.construct_sequence(node)}
    else:
        raise yaml.constructor.ConstructorError(
            None, None, f"Unexpected node type for !GetAtt: {type(node)}", node.start_mark
        )


CloudFormationLoader.add_constructor(
    "!Ref", lambda loader, node: {"Ref": loader.construct_scalar(node)}  # type: ignore
)
CloudFormationLoader.add_constructor(
    "!Sub", lambda loader, node: {"Fn::Sub": loader.construct_scalar(node)}  # type: ignore
)
CloudFormationLoader.add_constructor("!GetAtt", construct_getatt)


def load_template() -> Dict[str, Any]:
    with open("template.yaml", "r") as file:
        return yaml.load(file, Loader=CloudFormationLoader)


def export_endpoints(template: Dict[str, Any]) -> List[Dict[str, str]]:
    endpoints = []
    resources = template.get("Resources", {})
    for resource_name, resource in resources.items():
        if resource.get("Type") == "AWS::Serverless::Function":
            properties = resource.get("Properties", {})
            events = properties.get("Events", {})
            for event_name, event in events.items():
                if event.get("Type") == "Api":
                    api_props = event.get("Properties", {})
                    path = api_props.get("Path")
                    method = api_props.get("Method")
                    handler = properties.get("Handler")
                    code_uri = properties.get("CodeUri")

                    if path and method and handler and code_uri:
                        endpoints.append(
                            {
                                "path": path,
                                "method": method,
                                "handler": handler,
                                "code_uri": code_uri,
                                "resource_name": resource_name,
                            }
                        )
    return endpoints


def add_layers_to_path(template: Dict[str, Any]):
    """Add layers to path. Reads the template and adds the layers to the path. For easier imports."""
    resources = template.get("Resources", {})
    for _, resource in resources.items():
        if resource.get("Type") == "AWS::Serverless::LayerVersion":
            layer_path = resource.get("Properties", {}).get("ContentUri")
            if layer_path:
                full_path = os.path.join(os.getcwd(), layer_path)
                if full_path not in sys.path:
                    sys.path.append(full_path)
                    print(f"Added layer path: {full_path}")


class APIResponse(BaseModel):
    statusCode: int
    body: str
    headers: Dict[str, str] = {}


def setup_routes(template: Dict[str, Any]):
    endpoints = export_endpoints(template)
    for endpoint in endpoints:
        setup_route(
            endpoint["path"],
            endpoint["method"],
            endpoint["handler"],
            endpoint["code_uri"],
            endpoint["resource_name"],
        )


def setup_route(path: str, method: str, handler: str, code_uri: str, resource_name: str):
    module_name, function_name = handler.rsplit(".", 1)
    module_path = os.path.join(code_uri, f"{module_name}.py")
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise Exception(f"Module {module_name} not found in {code_uri}")
    module = importlib.util.module_from_spec(spec)

    spec.loader.exec_module(module)
    handler_function = getattr(module, function_name)

    path = path.replace("{", "<").replace("}", ">")

    print(f"Setting up route for [{method}] {path} with handler {resource_name}.")

    # Create a unique route handler for each Lambda function
    def create_route_handler(handler_func):
        def route_handler(*args, **kwargs):
            event = {
                "httpMethod": request.method,
                "path": request.path,
                "queryStringParameters": request.args.to_dict(),
                "headers": dict(request.headers),
                "body": request.get_data(as_text=True),
                "pathParameters": kwargs,
            }
            context = LambdaContext(resource_name)
            response = handler_func(event, context)

            try:
                api_response = APIResponse(**response)
                headers = response.get("headers", {})
                return Response(
                    api_response.body,
                    status=api_response.statusCode,
                    headers=headers,
                    mimetype="application/json",
                )
            except ValidationError as e:
                return jsonify({"error": "Invalid response format", "details": e.errors()}), 500

        return route_handler

    # Use a unique endpoint name for each route
    endpoint_name = f"{resource_name}_{method}_{path.replace('/', '_')}"
    app.add_url_rule(
        path,
        endpoint=endpoint_name,
        view_func=create_route_handler(handler_function),
        methods=[method.upper(), "OPTIONS"],
    )


if __name__ == "__main__":
    template = load_template()
    add_layers_to_path(template)
    setup_routes(template)
    app.run(debug=True, port=3000)
