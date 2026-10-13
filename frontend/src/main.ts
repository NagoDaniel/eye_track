import "./ui/style.css";
import { APP_NAME } from "./app";
import { Api } from "./api";
import { runApp } from "./flow";

document.title = APP_NAME;
const root = document.getElementById("app");
if (root === null) throw new Error("index.html has no #app element");
void runApp(root, new Api());
